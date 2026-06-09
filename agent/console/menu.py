"""Interactive start menu (arrow-key navigable).

Shown when the agent is launched with no task (``agent --menu``, which is what
``start.cmd`` / ``start.sh`` call). Lets you start a chat, manage the in-app
scheduler (recurring tasks that fire while the agent is open), edit ``.env``
settings (provider · model · API key · base URL), run the HTTP server with a live
request monitor, or quit — all with ↑/↓ + Enter. Falls back to numbered input
when the terminal isn't interactive.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from . import display

CORAL = "#d95767"
console = Console()
PROVIDERS = ["openai", "anthropic", "openrouter", "ollama"]


# ── Key reading (stdlib, cross-platform) ─────────────────────────────────────

def _read_key() -> str | None:
    """Return 'up' | 'down' | 'enter' | 'esc' | a character, or None."""
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):           # arrow / function key prefix
            ch2 = msvcrt.getch()
            return {b"H": "up", b"P": "down"}.get(ch2)
        if ch == b"\x1b":                       # Esc (standalone on Windows)
            return "esc"
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b"\x03":
            raise KeyboardInterrupt
        return ch.decode("latin-1", "ignore").lower()

    import select as _select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":                       # Esc, or the start of an arrow sequence
            ready, _, _ = _select.select([sys.stdin], [], [], 0.05)
            if not ready:
                return "esc"                   # bare Esc — nothing follows
            seq = sys.stdin.read(2)
            return {"[A": "up", "[B": "down"}.get(seq, "esc")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render(title: str, subtitle: str, options: list[str], index: int) -> None:
    console.clear()
    head = f"\n  [bold {CORAL}]micro-agent[/]"
    if subtitle:
        head += f"   [dim]{subtitle}[/]"
    console.print(head)
    console.print(f"  [dim]{title}[/]\n")
    for i, opt in enumerate(options):
        if i == index:
            console.print(f"  [{CORAL}]❯[/] [bold {CORAL}]{opt}[/]")
        else:
            console.print(f"    [dim]{opt}[/]")
    console.print("\n  [dim]↑/↓ move · Enter select · Esc back[/]")


def _edit_line(prompt: str, initial: str = "") -> str | None:
    """Read a line pre-filled with *initial*, returning the edited text.

    Supports typing and Backspace. Enter saves; Esc cancels (returns None).
    Redraws with \\r and \\b only, so no ANSI/VT support is required.
    """
    if not sys.stdin.isatty():
        try:
            typed = input(prompt)
        except EOFError:
            return None
        return typed if typed != "" else None

    buf = list(initial)
    prev_len = 0

    def redraw() -> None:
        nonlocal prev_len
        text = prompt + "".join(buf)
        pad = max(0, prev_len - len(text))
        sys.stdout.write("\r" + text + " " * pad + "\b" * pad)
        sys.stdout.flush()
        prev_len = len(text)

    if os.name == "nt":
        import msvcrt

        redraw()
        while True:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):       # arrow/function prefix — consume, ignore
                msvcrt.getwch()
                continue
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                return "".join(buf)
            if ch == "\x1b":                 # Esc — cancel
                sys.stdout.write("\n")
                return None
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x08":                 # Backspace
                if buf:
                    buf.pop()
            else:
                buf.append(ch)
            redraw()

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        redraw()
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stdout.write("\r\n")
                return "".join(buf)
            if ch == "\x1b":                 # Esc — cancel
                sys.stdout.write("\r\n")
                return None
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x7f", "\x08"):       # Backspace / Delete
                if buf:
                    buf.pop()
            else:
                buf.append(ch)
            redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def select(title: str, options: list[str], subtitle: str = "", index: int = 0) -> int | None:
    """Arrow-key selection. Returns the chosen index, or None on q/cancel."""
    if not sys.stdin.isatty():                 # non-interactive: numbered fallback
        console.print(f"\n  {title}")
        for i, opt in enumerate(options, 1):
            console.print(f"  {i}. {opt}")
        try:
            raw = input("  select> ").strip()
        except EOFError:
            return None
        return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(options) else None

    while True:
        _render(title, subtitle, options, index)
        try:
            key = _read_key()
        except KeyboardInterrupt:
            return None
        if key == "up":
            index = (index - 1) % len(options)
        elif key == "down":
            index = (index + 1) % len(options)
        elif key == "enter":
            return index
        elif key == "esc":
            return None


# ── .env read / write ────────────────────────────────────────────────────────

def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _set_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, found = [], False
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    # Reflect immediately so the next load_config() in this process sees it.
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)


def _mask(value: str) -> str:
    if not value:
        return "(unset)"
    return value if len(value) <= 8 else value[:6] + "…" + value[-4:]


# ── Actions ──────────────────────────────────────────────────────────────────

def _status(root: Path) -> str:
    env = _read_env(root / ".env")
    provider = env.get("PROVIDER") or os.getenv("PROVIDER") or "openai"
    model = env.get("MODEL") or os.getenv("MODEL") or "(default)"
    return f"{provider} · {model}"


def _chat(root) -> None:
    from ..runtime.config import load_config
    from ..runtime.context import build_deps, close_deps
    from ..engine.factory import build_agent
    from ..__main__ import _repl

    console.clear()
    config = load_config(root)
    agent = build_agent(config)
    deps = build_deps(config)
    try:
        _repl(agent, config, deps)
    except Exception as exc:  # noqa: BLE001 - keep the menu alive
        display.err(str(exc))
        input("  press Enter to return…")
    finally:
        close_deps(deps)


def _serve(root) -> None:
    """Run the HTTP server with a clean live request feed (read-only).

    No input prompt here — a prompt would fight the background feed for the
    cursor. You send requests from a browser (``/task?q=...``), curl, or another
    terminal; they all appear in the feed. Ctrl+C stops and returns to the menu.
    """
    from ..runtime.config import load_config
    from ..server import serve

    console.clear()
    try:
        raw = input("  Port [8181]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    try:
        port = int(raw) if raw else 8181
    except ValueError:
        port = 8181

    config = load_config(root)
    monitor = display.ServerMonitor(config.agent_name, port)
    console.clear()
    try:
        serve(config, port=port, monitor=monitor)   # blocks until Ctrl+C; cleans up
    except KeyboardInterrupt:
        pass
    except OSError as exc:                            # e.g. port already in use
        display.err(f"could not start server on :{port} — {exc}")
        input("  press Enter to return…")
        return
    monitor.print_stats()                            # closing summary on the way out


def _settings(root: Path) -> None:
    env_file = root / ".env"
    while True:
        env = _read_env(env_file)
        provider = env.get("PROVIDER", "(openai)")
        options = [
            f"Provider   · {provider}",
            f"Model      · {env.get('MODEL', '(default)')}",
            f"API key    · {_mask(env.get('API_KEY', ''))}",
            f"Base URL   · {env.get('BASE_URL', '') or '(auto)'}",
            "Back",
        ]
        choice = select("Settings  —  written to .env", options, subtitle=_status(root))
        if choice == 0:
            p = select("Provider", PROVIDERS + ["Back"])
            if p is not None and p < len(PROVIDERS):
                _set_env(env_file, "PROVIDER", PROVIDERS[p])
        elif choice == 1:
            _prompt_set(env_file, "MODEL", "Model id (e.g. gpt-4o-mini, llama3.1:8b)")
        elif choice == 2:
            _prompt_set(env_file, "API_KEY", "API key for the provider")
        elif choice == 3:
            _prompt_set(env_file, "BASE_URL", "Base URL (blank = provider default)")
        else:
            return


def _prompt_set(env_file: Path, key: str, label: str) -> None:
    current = _read_env(env_file).get(key, "")
    console.clear()
    console.print(f"\n  [bold]{label}[/]")
    console.print("  [dim]Edit the value, then Enter to save · Esc to cancel.[/]\n")
    try:
        value = _edit_line(f"  {key}=", current)   # pre-filled with current value
    except KeyboardInterrupt:
        return
    if value is None:        # Esc / cancelled — keep current
        return
    _set_env(env_file, key, value.strip())


# ── In-app scheduler ─────────────────────────────────────────────────────────
# Recurring tasks that fire ONLY while the agent is open (the live loop). Jobs
# persist in the state store, so they're remembered between sessions. For runs
# that must fire even when the terminal is closed, use an external scheduler
# (cron / Task Scheduler) — see schedule.example.

_INTERVALS = [
    ("every 30 seconds", 30),
    ("every 1 minute", 60),
    ("every 5 minutes", 300),
    ("every 15 minutes", 900),
    ("every 1 hour", 3600),
]
_JOBS_KEY = "scheduled_jobs"


def _fmt_every(secs: int) -> str:
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs % 60 == 0:
        return f"{secs // 60}m"
    return f"{secs}s"


def _clip(text: str, n: int) -> str:
    text = str(text).replace("\n", " ").strip()
    return (text[: n - 1] + "…") if len(text) > n else text


def _scheduler(root) -> None:
    from ..runtime.config import load_config
    from ..runtime.context import build_deps, close_deps
    from ..engine.factory import build_agent

    config = load_config(root)
    deps = build_deps(config)
    jobs: list[dict] = deps.store.get(_JOBS_KEY, []) or []

    try:
        while True:
            summary = (
                "  ·  ".join(f"{_fmt_every(j['every'])}: {_clip(j['task'], 18)}" for j in jobs)
                if jobs else "no jobs yet"
            )
            options = (["Run scheduler (live)"] if jobs else []) + ["Add job"]
            if jobs:
                options.append("Remove job")
            options.append("Back")

            choice = select("Scheduler", options, subtitle=_clip(summary, 66))
            picked = options[choice] if choice is not None else "Back"

            if picked == "Run scheduler (live)":
                agent = build_agent(config)
                _run_scheduler_live(agent, deps, jobs, config.model)
            elif picked == "Add job":
                _add_job(deps, jobs)
            elif picked == "Remove job":
                _remove_job(deps, jobs)
            else:
                return
    finally:
        close_deps(deps)


def _add_job(deps, jobs: list[dict]) -> None:
    console.clear()
    console.print("\n  [bold]New scheduled task[/]")
    console.print("  [dim]What should the agent do? Enter to confirm · Esc to cancel.[/]\n")
    task = _edit_line("  task= ", "")
    if not task or not task.strip():
        return
    every = select("How often?", [label for label, _ in _INTERVALS] + ["custom (seconds)…"])
    if every is None:
        return
    if every < len(_INTERVALS):
        secs = _INTERVALS[every][1]
    else:
        raw = _edit_line("  seconds= ", "60") or "60"
        try:
            secs = max(5, int(raw.strip()))
        except ValueError:
            secs = 60
    jobs.append({"task": task.strip(), "every": secs})
    deps.store.set(_JOBS_KEY, jobs)


def _remove_job(deps, jobs: list[dict]) -> None:
    labels = [f"every {_fmt_every(j['every'])}  ·  {_clip(j['task'], 40)}" for j in jobs]
    pick = select("Remove which job?", labels + ["Cancel"])
    if pick is not None and pick < len(jobs):
        jobs.pop(pick)
        deps.store.set(_JOBS_KEY, jobs)


def _run_scheduler_live(agent, deps, jobs: list[dict], model: str) -> None:
    """Passive feed: fire each job on its interval until Ctrl+C. No input prompt."""
    console.clear()
    lines = "\n".join(
        f"[dim]every {_fmt_every(j['every']):>4}[/]  {_clip(j['task'], 56)}" for j in jobs
    )
    console.print(
        Panel(
            f"{lines}\n[dim]· Ctrl+C to stop[/]",
            border_style=CORAL,
            title="[dim]scheduler running[/]",
        )
    )
    next_run = [time.monotonic() for _ in jobs]   # all due immediately on start
    try:
        while True:
            now = time.monotonic()
            for i, job in enumerate(jobs):
                if now >= next_run[i]:
                    _run_job(agent, deps, job)
                    next_run[i] = time.monotonic() + job["every"]
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n  [dim]scheduler stopped[/]\n")


def _run_job(agent, deps, job: dict) -> None:
    import asyncio

    task = job["task"]
    console.print(f"  [dim]{time.strftime('%H:%M:%S')}[/] [{CORAL}]→[/] {_clip(task, 60)}")
    start = time.monotonic()

    async def _run():
        async with agent:                  # starts/stops MCP servers if any
            return await agent.run(task, deps=deps)

    try:
        result = asyncio.run(_run())
        elapsed = time.monotonic() - start
        console.print(
            f"           [green]←[/] [dim]{elapsed:.1f}s[/]  {_clip(result.output, 80)}"
        )
    except Exception as exc:  # noqa: BLE001 - one bad run shouldn't stop the loop
        console.print(f"           [red]←[/] [dim]error: {_clip(str(exc), 60)}[/]")


# ── Main loop ────────────────────────────────────────────────────────────────

def run(root: str | None = None) -> int:
    root_path = Path(root or os.getcwd()).resolve()
    while True:
        choice = select(
            "Main menu",
            [
                "Chat with the agent",
                "Scheduler",
                "Settings",
                "Serve (HTTP + live monitor)",
                "Create a new agent",
                "Quit",
            ],
            subtitle=_status(root_path),
        )
        if choice == 0:
            _chat(root)
        elif choice == 1:
            _scheduler(root)
        elif choice == 2:
            _settings(root_path)
        elif choice == 3:
            _serve(root)
        elif choice == 4:
            from . import wizard

            wizard.run_wizard(root)
        else:
            console.clear()
            return 0
