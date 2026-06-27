"""The majestic-style console, built on ``rich``.

A live reasoning tree (REASON → tool call → result steps), a spinner while the
model thinks, compact tool call/result lines, and an ``inline_stats`` footer
(tokens · cost · elapsed). It hooks into Pydantic AI's streamed run events via
``Agent.iter``.

CLI-only by design: the core ``Agent`` and ``server.py`` never import this
module, so headless and server runs stay free of ``rich``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..runtime.config import Config
from ..runtime.context import AgentDeps
from ..runtime.runlog import append_run
from ..runtime.attachments import prompt_text
from ..engine.registry import tool_names
from ..engine.runner import Done, Reason, ToolCall, ToolResult, iter_events

EMERALD = "#10b981"
console = Console()

# The live spinner currently running (if any), so a confirm prompt can pause it.
_active_status: Any = None


def setup_logging(level: int = logging.INFO) -> None:
    """Route the ``agent`` logger tree through the rich console (CLI only).

    Engine/runtime modules log via ``logging.getLogger("agent.*")`` and never
    print; this attaches the one rich handler so those records render nicely in
    interactive runs. The server path never calls this — it stays on plain
    ``logging`` (and never imports this module).
    """
    from rich.logging import RichHandler

    root = logging.getLogger("agent")
    if any(isinstance(h, RichHandler) for h in root.handlers):
        return  # already configured (REPL restarted from the menu, etc.)
    handler = RichHandler(
        console=console, show_time=False, show_path=False, markup=False
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(level)


# ── Banner ──────────────────────────────────────────────────────────────────

LOGO = r"""[bold {c}]
  ╔═╗╔═╗╔╗╔╔═╗╔═╗╦╔═╗   ╔═╗╔═╗╔═╗╔╗╔╔╦╗
  ║ ╦║╣ ║║║║╣ ╚═╗║╚═╗───╠═╣║ ╦║╣ ║║║ ║
  ╚═╝╚═╝╝╚╝╚═╝╚═╝╩╚═╝   ╩ ╩╚═╝╚═╝╝╚╝ ╩
[/]""".format(c=EMERALD)


def print_banner(config: Config, tools: list) -> None:
    """Two-panel startup banner: identity + capabilities."""
    console.print(LOGO)

    api_ok = bool(config.api_key) or config.provider == "ollama"
    dot = "[green]●[/]" if api_ok else "[red]●[/]"

    left = Table.grid(padding=(0, 1))
    left.add_column(style="dim", justify="right")
    left.add_column()
    left.add_row("agent", f"[bold]{config.agent_name}[/]")
    left.add_row("provider", f"[{EMERALD}]{config.provider}[/]")
    left.add_row("model", config.model)
    fallbacks = config.settings.get("model_fallbacks") or []
    if fallbacks:
        left.add_row("fallback", f"[dim]{', '.join(str(m) for m in fallbacks)}[/]")
    left.add_row("api", f"{dot} {'ok' if api_ok else 'missing key'}")
    left.add_row("workspace", f"[dim]{_short(config.workspace)}[/]")
    if config.model_settings:
        tuning = ", ".join(f"{k}={v}" for k, v in config.model_settings.items())
        left.add_row("tuning", f"[dim]{tuning}[/]")
    if config.usage_limits is not None:
        left.add_row("limits", f"[dim]{_limits_summary(config.usage_limits)}[/]")

    names = tool_names(tools)
    right = Table.grid(padding=(0, 1))
    right.add_column(style="dim", justify="right")
    right.add_column()
    right.add_row("tools", f"[bold]{len(names)}[/] registered")
    right.add_row("", Text(", ".join(names), style="dim", overflow="fold"))
    if config.settings:
        keys = ", ".join(list(config.settings.keys())[:6])
        right.add_row("settings", f"[dim]{keys}[/]")

    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(
        Panel(left, title="[dim]identity[/]", border_style=EMERALD),
        Panel(right, title="[dim]capabilities[/]", border_style="dim"),
    )
    console.print(grid)
    console.print(
        f"  [dim]Type a task, or [/][bold]/help[/][dim] · "
        f"[/][bold]/quit[/][dim] to exit[/]\n"
    )


def _short(p: Path | str, n: int = 36) -> str:
    s = str(p)
    return "…" + s[-(n - 1):] if len(s) > n else s


def _limits_summary(limits: Any) -> str:
    """Compact one-line view of the set fields on a ``UsageLimits``."""
    fields = ("request_limit", "total_tokens_limit", "tool_calls_limit")
    parts = [
        f"{f}={getattr(limits, f)}"
        for f in fields
        if getattr(limits, f, None) is not None
    ]
    return ", ".join(parts) or "set"


# ── Streamed run with live tree ─────────────────────────────────────────────

async def run_streamed(
    agent: Agent, task: str, deps: AgentDeps, model: str = "", message_history=None
) -> Any:
    """Run *agent* on *task*, rendering the live tree, and return the result.

    *message_history* (optional) is a list of prior Pydantic AI messages — the
    REPL passes it so the model sees earlier turns; one-shot/server omit it.

    Returns the Pydantic AI ``AgentRunResult`` (``.output`` / ``.usage`` /
    ``.new_messages()``).
    """
    start = time.monotonic()
    step = {"n": 0}
    result = None
    task_text = prompt_text(task)  # text form for the run log (not image bytes)
    deps.extra.pop("plan", None)  # the todo scratchpad is per-task (Phase 13)

    status = console.status(f"[{EMERALD}]Thinking…", spinner="dots")
    global _active_status
    _active_status = status
    status.start()
    try:
        # Entering the agent context starts any MCP servers (no-op without them);
        # `iter_events` (shared with the server's SSE path) walks the run.
        async with agent:
            async for ev in iter_events(agent, task, deps, message_history=message_history):
                if isinstance(ev, Reason):
                    status.stop()
                    _reason(ev.text, step)
                    status.start()
                elif isinstance(ev, ToolCall):
                    status.update(f"[{EMERALD}]{ev.name}…")
                elif isinstance(ev, ToolResult):
                    status.stop()
                    if ev.name == "update_plan":
                        _plan_block(ev.content, step)
                    else:
                        _tool_line(ev.name, ev.args, ev.content, step)
                    status.start()
                elif isinstance(ev, Done):
                    result = ev.result
    except Exception as exc:
        append_run(deps, task_text, time.monotonic() - start, 0, ok=False, error=str(exc))
        raise
    finally:
        status.stop()
        _active_status = None

    _tree_close(step)
    # `result.usage` is a property in current Pydantic AI; older versions
    # exposed it as a method. Use the object directly when it already looks
    # like a usage record, else call it.
    u = result.usage
    usage = u if hasattr(u, "input_tokens") else u()
    elapsed = time.monotonic() - start
    inline_stats(usage, elapsed, model)
    total = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    append_run(deps, task_text, elapsed, total, ok=True)
    return result


def _prefix(step: dict) -> str:
    p = "┌" if step["n"] == 0 else "├"
    step["n"] += 1
    return p


def _reason(text: str, step: dict) -> None:
    first = text.strip().split("\n", 1)[0].strip()
    if len(first) > 88:
        first = first[:87] + "…"
    console.print(f"  [{EMERALD}]{_prefix(step)}[/] [bold]REASON[/]  [dim]{_esc(first)}[/]")


def _tool_line(name: str, args: Any, result: Any, step: dict) -> None:
    detail = "  ·  ".join(
        p for p in (_args_summary(name, args), _result_summary(result)) if p
    )
    line = f"  [{EMERALD}]{_prefix(step)}[/] [bold]{name}[/]"
    if detail:
        line += f"  [dim]·[/]  [dim]{_esc(detail)}[/]"
    console.print(line)


def _plan_block(content: str, step: dict) -> None:
    """Render the todo checklist (update_plan's result) as a small block."""
    console.print(f"  [{EMERALD}]{_prefix(step)}[/] [bold]plan[/]")
    colors = {"○": "dim", "▸": EMERALD, "✓": "green"}
    for line in str(content).splitlines():
        console.print(f"       [{colors.get(line[:1], 'dim')}]{_esc(line)}[/]")


def _tree_close(step: dict) -> None:
    if step["n"] > 0:
        console.print(f"  [green]└[/] [dim]done[/]")


def inline_stats(usage: Any, elapsed: float, model: str = "") -> None:
    """Compact footer: tokens · cache · cost · elapsed."""
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    total = inp + out
    cost = _estimate_cost(inp, out, model)
    cost_str = f"  ·  [green]${cost:.4f}[/]" if cost else ""
    # Prompt-cache reads (Phase 16) — visible confirmation that caching is working.
    cache_read = getattr(usage, "cache_read_tokens", 0) or 0
    cache_str = f"  ·  [green]{cache_read:,} cached[/]" if cache_read else ""
    console.print(
        f"  [dim]↳  {total:,} tok ([dim]{inp:,}→{out:,}[/])"
        f"{cache_str}{cost_str}  ·  {elapsed:.1f}s[/]\n"
    )


# ── Summaries (kept short for the tree) ─────────────────────────────────────

def _as_dict(args: Any) -> dict:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            d = json.loads(args)
            return d if isinstance(d, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _trunc(s: str, n: int = 40) -> str:
    s = str(s).replace("\n", " ").strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def _args_summary(name: str, args: Any) -> str:
    a = _as_dict(args)
    if name in ("read_file", "write_file"):
        p = Path(str(a.get("path", ""))).name
        if name == "write_file":
            return f"{p} · {len(str(a.get('content', '')))} chars" if p else ""
        return p
    if name == "list_dir":
        return str(a.get("path", ".")) or "."
    if name == "run_shell":
        return _trunc(str(a.get("command", "")), 44)
    if name == "fetch_url":
        return _trunc(str(a.get("url", "")).replace("https://", "").replace("http://", ""), 44)
    if a:
        return _trunc(str(next(iter(a.values()))), 40)
    return ""


def _result_summary(result: Any) -> str:
    if isinstance(result, (list, tuple)):
        n = len(result)
        return f"{n} item{'s' if n != 1 else ''}"
    if isinstance(result, dict):
        return "{" + ", ".join(list(result.keys())[:3]) + "}"
    return _trunc(str(result), 48)


def _esc(s: str) -> str:
    return s.replace("[", r"\[")


# ── Cost estimate (best-effort) ─────────────────────────────────────────────

# USD per 1M tokens (input, output). Substring match on the model id.
_PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
    "o4-mini": (1.10, 4.40),
    "claude-haiku": (1.0, 5.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-opus": (15.0, 75.0),
}


def _estimate_cost(inp: int, out: int, model: str) -> float:
    model = (model or "").lower()
    for key, (pin, pout) in _PRICES.items():
        if key in model:
            return inp / 1e6 * pin + out / 1e6 * pout
    return 0.0


# ── Plain helpers ───────────────────────────────────────────────────────────

def ok(msg: str) -> None:
    console.print(f"  [green]✓[/] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [yellow]⚠[/] {msg}")


def err(msg: str) -> None:
    console.print(f"  [red]✗[/] {msg}")


def info(msg: str) -> None:
    console.print(f"  [dim]{msg}[/]")


def answer(text: str) -> None:
    """Render the agent's final answer."""
    console.print(Panel(Text(str(text)), border_style="green", title="[dim]answer[/]"))


def confirm_tool(name: str, rendered_args: str) -> bool:
    """Interactive y/N gate for confirm-listed tools (the CLI ``confirm_hook``).

    Pauses the live spinner (it would fight the prompt for the cursor), asks, and
    resumes. A bare Enter, EOF, or Ctrl+C declines — the safe default.
    """
    paused = _active_status
    if paused is not None:
        paused.stop()
    try:
        detail = f"  [dim]{_esc(_trunc(rendered_args, 80))}[/]" if rendered_args else ""
        console.print(f"  [yellow]confirm[/] run [bold]{name}[/]{detail}")
        try:
            resp = console.input("  approve? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return False
        return resp in ("y", "yes")
    finally:
        if paused is not None:
            paused.start()


def approve_action(subject: str, detail: str) -> str:
    """3-way approval prompt (the CLI ``approval_hook``) → 'once'|'always'|'deny'.

    Pauses the live spinner, asks, resumes. Default (Enter / EOF / Ctrl+C) is
    deny — the safe answer. Used for confirm-listed tool calls and the
    activation of agent-written tools (Phase 11).
    """
    paused = _active_status
    if paused is not None:
        paused.stop()
    try:
        extra = f"  [dim]{_esc(_trunc(detail, 80))}[/]" if detail else ""
        console.print(f"  [yellow]approve[/] [bold]{_esc(subject)}[/]{extra}")
        try:
            resp = console.input("  [o]nce · [a]lways · [d]eny (default deny): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return "deny"
        if resp in ("a", "always"):
            return "always"
        if resp in ("o", "once", "y", "yes"):
            return "once"
        return "deny"
    finally:
        if paused is not None:
            paused.start()


# ── Server monitor (live request feed for `--serve` from the menu) ───────────

class ServerMonitor:
    """A rich live feed + running stats for the interactive serve console.

    Passed to :func:`agent.server.serve` / ``start_background``; the server calls
    these hooks (it never imports rich itself, so headless/Docker stays clean).
    Each request prints an arrival line and a completion line; counters feed
    :meth:`print_stats`.
    """

    def __init__(self, agent_name: str, port: int, host: str = "127.0.0.1"):
        self.agent_name = agent_name
        self.port = port
        self.host = host
        self.started = time.time()
        self.requests = 0
        self.errors = 0
        self.tokens = 0
        self.total_time = 0.0
        self._lock = threading.Lock()

    def on_start(self) -> None:
        console.print(
            Panel(
                f"[bold]{self.agent_name}[/] serving on "
                f"[bold]http://{self.host}:{self.port}[/]   [dim]· Ctrl+C to stop[/]\n"
                f"[dim]browser  http://localhost:{self.port}/task?q=hi[/]\n"
                f"[dim]health   http://localhost:{self.port}/health[/]\n"
                f"[dim]curl     curl -X POST localhost:{self.port}/task "
                f"-d '{{\"task\":\"hi\"}}'[/]",
                border_style=EMERALD,
                title="[dim]server monitor[/]",
            )
        )

    def on_request(self, task: str, client: str = "") -> None:
        with self._lock:
            self.requests += 1
        ts = time.strftime("%H:%M:%S")
        line = f"  [dim]{ts}[/] [{EMERALD}]→[/] {_esc(_trunc(task, 60))}"
        if client:
            line += f"  [dim]({client})[/]"
        console.print(line)

    def on_result(self, ok: bool, tokens: int, elapsed: float) -> None:
        with self._lock:
            if not ok:
                self.errors += 1
            self.tokens += tokens
            self.total_time += elapsed
        mark = "[green]←[/]" if ok else "[red]←[/]"
        status = "ok" if ok else "error"
        console.print(
            f"           {mark} [dim]{status} · {tokens:,} tok · {elapsed:.1f}s[/]"
        )

    def on_access(self, method: str, path: str, status: int, client: str = "") -> None:
        """Compact log line for any non-/task request (health, 404, …)."""
        ts = time.strftime("%H:%M:%S")
        color = "green" if status < 400 else ("yellow" if status < 500 else "red")
        line = (
            f"  [dim]{ts}[/] [dim]{method}[/] {_esc(_trunc(path, 40))}  "
            f"[{color}]{status}[/]"
        )
        if client:
            line += f"  [dim]({client})[/]"
        console.print(line)

    def print_stats(self) -> None:
        up = int(time.time() - self.started)
        h, rem = divmod(up, 3600)
        m, s = divmod(rem, 60)
        uptime = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")
        avg = self.total_time / self.requests if self.requests else 0.0
        ok = self.requests - self.errors
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", justify="right")
        grid.add_column()
        grid.add_row("uptime", uptime)
        grid.add_row("requests", f"[bold]{self.requests}[/]  ([green]{ok} ok[/], "
                                 f"{'[red]' if self.errors else '[dim]'}{self.errors} err[/])")
        grid.add_row("tokens", f"{self.tokens:,}")
        grid.add_row("avg time", f"{avg:.1f}s")
        console.print(Panel(grid, border_style="dim", title="[dim]stats[/]"))
