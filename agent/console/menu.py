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

EMERALD = "#15c17c"
console = Console()
PROVIDERS = ["openai", "anthropic", "openrouter", "ollama"]
_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "openrouter": "openai/gpt-4o-mini",
    "ollama": "llama3.1:8b",
}


def _clear() -> None:
    """Hard-clear the terminal — screen AND scrollback, then home the cursor.

    rich's ``Console.clear()`` emits only ``ESC[2J`` (clear visible screen) +
    home, which several terminals (VS Code, Windows Terminal) leave with stale
    frames in the scrollback. Adding ``ESC[3J`` wipes the scrollback so menu
    screens don't pile up. Used for full screen transitions; the arrow-select
    loop redraws in place instead (see ``_render``) to avoid flicker. Written
    through the console's own stream so ordering with rich output is preserved.
    """
    console.file.write("\033[2J\033[3J\033[H")
    console.file.flush()


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
    # Redraw IN PLACE — home the cursor and overwrite, then erase whatever is
    # left below. No full-screen wipe, so arrow navigation doesn't flicker.
    # The cursor is hidden during the redraw to avoid it jumping around.
    console.file.write("\033[?25l\033[H")
    console.file.flush()
    console.print(display.LOGO)
    if subtitle:
        console.print(f"  [dim]{subtitle}[/]")
    console.print(f"  [dim]{title}[/]\n")
    for i, opt in enumerate(options):
        if i == index:
            console.print(f"  [{EMERALD}]❯[/] [bold {EMERALD}]{opt}[/]")
        else:
            console.print(f"    [dim]{opt}[/]")
    console.print("\n  [dim]↑/↓ move · Enter select · Esc back[/]")
    console.file.write("\033[0J")   # erase any leftover from a longer prior frame
    console.file.flush()


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

    _clear()                                   # full clear once on entry
    try:
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
    finally:
        console.file.write("\033[?25h")        # always restore the cursor
        console.file.flush()


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

    _clear()
    config = load_config(root)
    agent = build_agent(config)
    deps = build_deps(config)
    deps.approval_hook = display.approve_action  # 3-way gate: confirm + activation
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

    _clear()
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
    _clear()
    try:
        serve(config, port=port, monitor=monitor)   # blocks until Ctrl+C; cleans up
    except KeyboardInterrupt:
        pass
    except OSError as exc:                            # e.g. port already in use
        display.err(f"could not start server on :{port} — {exc}")
        input("  press Enter to return…")
        return
    monitor.print_stats()                            # closing summary on the way out


def _subagents(root) -> None:
    """Read-only roster of the named subagents in ``workspace/agents/``.

    Lists every ``<name>.md`` definition; Enter on one shows its full persona +
    frontmatter. Viewing only — the agent authors subagents with ``write_agent``,
    or you edit the files directly. Still works when delegation is opted out
    (``subagents.enabled: false``), so you can review a roster before re-enabling.
    """
    from ..runtime.config import load_config
    from ..tools.subagents import load_specs

    config = load_config(root)
    enabled = bool((config.settings.get("subagents") or {}).get("enabled"))

    while True:
        specs = load_specs(config.workspace)
        if not specs:
            select(
                "Subagents  —  workspace/agents/",
                ["Back"],
                subtitle="no subagents yet — add workspace/agents/<name>.md or ask the agent to create one",
            )
            return
        note = f"{len(specs)} subagent(s)" + (
            "" if enabled else "   ·   delegation OFF — set subagents.enabled: true in settings.yaml"
        )
        options = [f"{s.name}  ·  {_clip(s.description, 48)}" for s in specs] + ["Back"]
        choice = select("Subagents  —  workspace/agents/", options, subtitle=note)
        if choice is None or choice >= len(specs):
            return
        _show_subagent(specs[choice], enabled)


def _show_subagent(spec, enabled: bool) -> None:
    """Render one subagent definition, then wait for Enter."""
    _clear()
    tools = (
        f"allow: {', '.join(spec.allow)}" if spec.allow
        else f"deny: {', '.join(spec.deny)}" if spec.deny
        else "inherits the parent's tools"
    )
    body = (
        f"[bold {EMERALD}]{spec.name}[/]\n"
        f"[dim]{spec.description or '(no description)'}[/]\n\n"
        f"[dim]tools:[/] {tools}\n"
        f"[dim]model:[/] {spec.model or 'inherits the parent model'}\n"
        f"[dim]call:[/]  delegate_to(\"{spec.name}\", task)"
        + ("" if enabled else "   [dim](delegation is off)[/]")
        + f"\n\n[dim]── persona ──[/]\n{spec.persona}"
    )
    console.print(Panel(body, border_style=EMERALD, title="[dim]subagent[/]"))
    try:
        input("\n  press Enter to return…")
    except (EOFError, KeyboardInterrupt):
        pass


def _pause() -> None:
    try:
        input("\n  press Enter to return…")
    except (EOFError, KeyboardInterrupt):
        pass


# ── Gateways (messaging channels) ─────────────────────────────────────────────

def _gw_label(config, name: str, cls=None) -> str:
    from ..gateways import manager

    if cls is not None and getattr(cls, "webhook_only", False):
        return f"{name}  ·  webhook-only (runs under --serve)"
    st = manager.status(config, name)
    return f"{name}  ·  " + (f"running (pid {st['pid']})" if st["running"] else "stopped")


def _gateways(root: Path) -> None:
    """Start/stop messaging channels (Telegram, …) and manage their access.

    Each channel runs as its **own** process (PID-tracked), so a bot started here
    keeps running after you leave the menu and works in parallel with the CLI.
    Gateways need the SQLite/WAL store; a JSON store is flagged here.
    """
    from ..runtime.config import load_config
    from ..runtime.context import build_deps, close_deps
    from ..gateways import discover_gateways
    from ..gateways.base import store_guard

    config = load_config(root)
    classes = discover_gateways(config)
    if not classes:
        select("Gateways", ["Back"], subtitle="no channels available in this agent")
        return
    deps = build_deps(config)
    try:
        names = sorted(classes)
        while True:
            warn = store_guard(deps.store)
            options = [_gw_label(config, n, classes[n]) for n in names] + ["Back"]
            sub = warn or "a channel runs as its own process — survives leaving the menu"
            choice = select("Gateways  —  messaging channels", options, subtitle=_clip(sub, 70))
            if choice is None or choice >= len(names):
                return
            _gateway_actions(config, deps, names[choice], classes[names[choice]])
    finally:
        close_deps(deps)


def _gateway_actions(config, deps, name: str, cls) -> None:
    from ..gateways import manager
    from ..gateways.base import store_guard

    env_file = config.root / ".env"
    token_env = getattr(cls, "token_env", "")
    owner_env = getattr(cls, "owner_env", "")
    webhook_only = getattr(cls, "webhook_only", False)

    while True:
        st = manager.status(config, name)
        running = st["running"]
        env = _read_env(env_file)
        token_val = (env.get(token_env) or os.getenv(token_env, "")) if token_env else ""

        # Webhook-only channels (WhatsApp) have no local process to start — they
        # mount on `agent --serve`; here you only manage keys and the allowlist.
        rows = [] if webhook_only else ["Stop" if running else "Start", "View log"]
        if token_env:
            rows.append(f"Token     · {_mask(token_val)}")
        if owner_env:
            rows.append(f"Owner id  · {env.get(owner_env, '') or os.getenv(owner_env, '') or '(unset)'}")
        rows += ["Manage allowlist", "Back"]

        guard = store_guard(deps.store)
        sub = guard or (
            "webhook-only — enable in settings.yaml and run `agent --serve`"
            if webhook_only else (f"running (pid {st['pid']})" if running else "stopped")
        )
        choice = select(f"Gateway · {name}", rows, subtitle=_clip(sub, 70))
        if choice is None:
            return
        picked = rows[choice]

        if picked == "Start":
            problem = store_guard(deps.store)
            if problem:
                display.err(problem)
                _pause()
            elif token_env and not token_val:
                display.warn(f"set the token first ({token_env}).")
                _pause()
            else:
                manager.start(config, name)
                display.ok(f"{name} started — see View log for output")
                _pause()
        elif picked == "Stop":
            stopped = manager.stop(config, name)
            display.ok(f"{name} stopped" if stopped else f"{name} was not running")
            _pause()
        elif picked == "View log":
            _view_log(config, name)
        elif token_env and picked.startswith("Token"):
            _prompt_set(env_file, token_env, f"{name} bot token")
        elif owner_env and picked.startswith("Owner"):
            _prompt_set(env_file, owner_env, f"{name} owner id (your numeric id)")
        elif picked == "Manage allowlist":
            _manage_allowlist(config, deps, name)
        else:
            return


def _view_log(config, name: str) -> None:
    from ..gateways import manager

    _clear()
    path = manager.log_path(config, name)
    console.print(f"\n  [dim]{path}[/]\n")
    if not path.exists():
        console.print("  [dim](no log yet — start the gateway first)[/]")
    else:
        try:
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        except OSError as exc:
            tail = [f"(could not read log: {exc})"]
        for line in tail:
            console.print(f"  [dim]{_clip(line, 100)}[/]")
    _pause()


def _manage_allowlist(config, deps, name: str) -> None:
    from ..gateways.base import AccessControl, gateway_settings

    seed = gateway_settings(config.settings, name).get("allowlist")
    ac = AccessControl(deps.store, name, seed)
    while True:
        ids = ac.listing()
        options = (["Add id", "Remove id", "Back"] if ids else ["Add id", "Back"])
        sub = ("allowed: " + ", ".join(ids)) if ids else "empty = deny-all (nobody can chat)"
        choice = select(f"Allowlist · {name}", options, subtitle=_clip(sub, 70))
        if choice is None:
            return
        picked = options[choice]
        if picked == "Add id":
            val = _edit_line("  id= ", "")
            if val and val.strip():
                ac.allow(val.strip())
        elif picked == "Remove id":
            pick = select("Remove which id?", ids + ["Cancel"])
            if pick is not None and pick < len(ids):
                if not ac.deny(ids[pick]):
                    display.warn("that id is seeded in settings.yaml — edit it there to remove")
                    _pause()
        else:
            return


def _check_updates(root) -> None:
    """Read-only: compare the local version against the newest tag on GitHub."""
    from ..runtime import updates

    _clear()
    cur = updates.current_version(root)
    console.print(f"\n  current version  [bold]{cur}[/]")
    with console.status(f"[{EMERALD}]checking GitHub…", spinner="dots"):
        latest = updates.latest_version()

    url = updates.repo_url()
    if latest is None:
        display.warn("couldn't determine the latest version (no tags found, or GitHub unreachable)")
    elif updates.is_newer(latest, cur):
        display.info(f"a newer version is available: [bold]{latest}[/]")
        console.print(f"  [dim]changelog  {url}/blob/main/CHANGELOG.md[/]")
        console.print(f"  [dim]project    {url}[/]")
        try:
            ans = input("  open the project page in your browser? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans in ("y", "yes"):
            import webbrowser

            webbrowser.open(url)
    else:
        display.ok(f"you're on the latest version ({cur})")

    try:
        input("\n  press Enter to return…")
    except (EOFError, KeyboardInterrupt):
        pass


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
    _clear()
    console.print(f"\n  [bold]{label}[/]")
    console.print("  [dim]Edit the value, then Enter to save · Esc to cancel.[/]\n")
    try:
        value = _edit_line(f"  {key}=", current)   # pre-filled with current value
    except KeyboardInterrupt:
        return
    if value is None:        # Esc / cancelled — keep current
        return
    _set_env(env_file, key, value.strip())


# ── In-app scheduler (menu live mode) ────────────────────────────────────────
# Recurring tasks persist in the store under the shared `scheduled_jobs` key
# (runtime/scheduler) — the same list the agent's schedule_task tool and the
# background ticker (gateway · server) use. This menu mode fires them live while
# it's open; for 24/7 firing without a terminal, run a bot / the server, or
# external cron (see schedule.example).

_INTERVALS = [
    ("every 30 seconds", 30),
    ("every 1 minute", 60),
    ("every 5 minutes", 300),
    ("every 15 minutes", 900),
    ("every 1 hour", 3600),
]


def _clip(text: str, n: int) -> str:
    text = str(text).replace("\n", " ").strip()
    return (text[: n - 1] + "…") if len(text) > n else text


def _scheduler(root) -> None:
    from ..runtime.config import load_config
    from ..runtime.context import build_deps, close_deps
    from ..engine.factory import build_agent
    from ..runtime import scheduler

    config = load_config(root)
    deps = build_deps(config)
    try:
        while True:
            jobs = scheduler.list_jobs(deps.store)
            summary = (
                "  ·  ".join(f"{scheduler.fmt_every(j['every'])}: {_clip(j['task'], 18)}" for j in jobs)
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
                _run_scheduler_live(agent, deps)
            elif picked == "Add job":
                _add_job(deps)
            elif picked == "Remove job":
                _remove_job(deps)
            else:
                return
    finally:
        close_deps(deps)


def _add_job(deps) -> None:
    from ..runtime import scheduler

    _clear()
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
            secs = max(scheduler.MIN_EVERY, int(raw.strip()))
        except ValueError:
            secs = 60
    scheduler.add_job(deps.store, task.strip(), secs, deliver="all")


def _remove_job(deps) -> None:
    from ..runtime import scheduler

    jobs = scheduler.list_jobs(deps.store)
    labels = [f"[{j['id']}] every {scheduler.fmt_every(j['every'])}  ·  {_clip(j['task'], 40)}" for j in jobs]
    pick = select("Remove which job?", labels + ["Cancel"])
    if pick is not None and pick < len(jobs):
        scheduler.remove_job(deps.store, jobs[pick]["id"])


def _run_scheduler_live(agent, deps) -> None:
    """Passive feed: fire each due job on its interval until Ctrl+C. No prompt."""
    from ..runtime import scheduler

    _clear()
    jobs = scheduler.list_jobs(deps.store)
    lines = "\n".join(
        f"[dim]every {scheduler.fmt_every(j['every']):>4}[/]  {_clip(j['task'], 56)}" for j in jobs
    )
    console.print(
        Panel(
            f"{lines}\n[dim]· Ctrl+C to stop[/]",
            border_style=EMERALD,
            title="[dim]scheduler running[/]",
        )
    )
    try:
        while True:
            for job in scheduler.due_jobs(deps.store):
                _run_job(agent, deps, job)
                scheduler.bump(deps.store, job["id"])
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n  [dim]scheduler stopped[/]\n")


def _run_job(agent, deps, job: dict) -> None:
    import asyncio

    from ..runtime.runlog import append_run
    from ..runtime.transcripts import write_transcript

    task = job["task"]
    console.print(f"  [dim]{time.strftime('%H:%M:%S')}[/] [{EMERALD}]→[/] {_clip(task, 60)}")
    start = time.monotonic()

    async def _run():
        async with agent:                  # starts/stops MCP servers if any
            return await agent.run(task, deps=deps, usage_limits=deps.config.usage_limits)

    try:
        result = asyncio.run(_run())
        elapsed = time.monotonic() - start
        console.print(
            f"           [green]←[/] [dim]{elapsed:.1f}s[/]  {_clip(result.output, 80)}"
        )
        u = result.usage
        usage = u if hasattr(u, "input_tokens") else u()
        tokens = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
        append_run(deps, task, elapsed, tokens, ok=True)
        write_transcript(deps, task, result=result, duration=elapsed, ok=True)
    except Exception as exc:  # noqa: BLE001 - one bad run shouldn't stop the loop
        elapsed = time.monotonic() - start
        console.print(f"           [red]←[/] [dim]error: {_clip(str(exc), 60)}[/]")
        append_run(deps, task, elapsed, 0, ok=False, error=str(exc))
        write_transcript(deps, task, duration=elapsed, ok=False, error=str(exc))


# ── First-run setup ───────────────────────────────────────────────────────────

def _needs_setup(root: Path) -> bool:
    """True when the agent has no usable credentials yet (a fresh install).

    Ollama needs no key, so it's always considered configured.
    """
    from ..runtime.config import load_config

    try:
        cfg = load_config(root)
    except Exception:  # noqa: BLE001 - unreadable config → treat as unconfigured
        return True
    if cfg.provider == "ollama":
        return False
    return not cfg.api_key


def _first_run_setup(root: Path) -> None:
    """Guided first-run config: provider → model → key. Writes ``.env``.

    Shown the first time ``start`` runs on an unconfigured agent, instead of the
    main menu. Reuses the same building blocks as Settings, so the agent is ready
    after a couple of choices — no hand-editing ``.env``.
    """
    env_file = root / ".env"
    _clear()
    console.print(display.LOGO)
    console.print("  [bold]Welcome — let's set up your agent.[/]")
    console.print("  [dim]A couple of choices; change them anytime in Settings.[/]\n")

    pick = select("Provider", PROVIDERS, subtitle="which model powers your agent")
    if pick is None:
        return  # cancelled — fall through to the menu, Settings is there too
    provider = PROVIDERS[pick]
    _set_env(env_file, "PROVIDER", provider)

    default_model = _DEFAULT_MODELS.get(provider, "")
    _clear()
    console.print(f"\n  [bold]Model[/]  [dim](provider: {provider})[/]")
    console.print("  [dim]Enter to accept the default · edit to change.[/]\n")
    model = _edit_line("  MODEL= ", default_model) or default_model
    _set_env(env_file, "MODEL", model.strip())

    if provider == "ollama":
        _set_env(env_file, "BASE_URL", "http://localhost:11434/v1")
        console.print("\n  [dim]Ollama runs locally — no API key needed.[/]")
    else:
        _clear()
        console.print("\n  [bold]API key[/]")
        console.print("  [dim]Paste your key, or leave blank to add later in Settings.[/]\n")
        key = _edit_line("  API_KEY= ", "")
        if key and key.strip():
            _set_env(env_file, "API_KEY", key.strip())

    display.ok("agent configured")
    try:
        input("  press Enter to open the menu... ")
    except (EOFError, KeyboardInterrupt):
        pass


# ── Main loop ────────────────────────────────────────────────────────────────

def run(root: str | None = None) -> int:
    root_path = Path(root or os.getcwd()).resolve()
    if _needs_setup(root_path):
        _first_run_setup(root_path)
    while True:
        choice = select(
            "Main menu",
            [
                "Chat with the agent",
                "Scheduler",
                "Subagents",
                "Gateways",
                "Settings",
                "Serve (HTTP + live monitor)",
                "Create a new agent",
                "Check for updates",
                "Quit",
            ],
            subtitle=_status(root_path),
        )
        if choice == 0:
            _chat(root)
        elif choice == 1:
            _scheduler(root)
        elif choice == 2:
            _subagents(root_path)
        elif choice == 3:
            _gateways(root_path)
        elif choice == 4:
            _settings(root_path)
        elif choice == 5:
            _serve(root)
        elif choice == 6:
            from . import wizard

            wizard.run_wizard(root)
        elif choice == 7:
            _check_updates(root_path)
        else:
            _clear()
            return 0
