"""Entrypoint: one-shot · REPL · ``--serve``.

A thin layer over the same Agent built by :func:`agent.engine.factory.build_agent`:

    agent "summarize the README"      # one-shot, rendered via the console
    agent                             # interactive REPL
    agent --serve --port 8181         # stdlib HTTP service (no rich)

The CLI renders through ``display``; ``--serve`` hands off to ``server.py``,
which never imports rich.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from pydantic_ai.exceptions import UsageLimitExceeded

from .console import display
from .runtime.config import load_config
from .runtime.context import build_deps, close_deps
from .runtime.attachments import (
    build_user_prompt, extract_attachments, max_mb_from, vision_hint,
)
from .engine.factory import build_agent
from .engine.registry import discover_tools


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent", description="genesis-agent — a modular Pydantic AI agent"
    )
    parser.add_argument("task", nargs="*", help="task to run (omit for REPL)")
    parser.add_argument("--menu", action="store_true", help="show the interactive start menu")
    parser.add_argument("--new", action="store_true", help="wizard to scaffold a new vertical agent")
    parser.add_argument("--serve", action="store_true", help="run as an HTTP service")
    parser.add_argument(
        "--gateway", default=None, metavar="NAME",
        help="run a messaging gateway in this process (e.g. telegram); usually "
             "started/stopped via the menu, not by hand",
    )
    parser.add_argument("--port", type=int, default=8181, help="port for --serve")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind address for --serve (default localhost; use 0.0.0.0 in containers)",
    )
    parser.add_argument("--root", default=None, help="agent folder (default: cwd)")
    parser.add_argument(
        "--session", default=None, metavar="ID",
        help="resume a persisted conversation thread by id (REPL only; needs "
             "threads.enabled in settings.yaml)",
    )
    parser.add_argument(
        "--image", action="append", default=None, metavar="PATH_OR_URL",
        help="attach an image/PDF (path or URL); repeatable. One-shot only — "
             "in the REPL just drag a file into the terminal.",
    )
    return parser.parse_args(argv)


def _force_utf8() -> None:
    """Box-drawing/spinner glyphs need UTF-8 stdout (notably on Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best effort
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # Logging: CLI paths render the `agent.*` loggers through the rich console;
    # --serve and --gateway stay on plain stdlib logging (headless, output tee'd
    # to a log file — no rich, no color codes).
    if args.serve or args.gateway:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    else:
        display.setup_logging()

    if args.new:
        from .console import wizard

        return wizard.run_wizard(args.root)

    if args.menu:
        from .console import menu

        return menu.run(args.root)

    config = load_config(args.root)

    if args.serve:
        from . import server

        return server.serve(config, port=args.port, host=args.host)

    if args.gateway:
        return _run_gateway(config, args.gateway)

    agent = build_agent(config)
    deps = build_deps(config)
    deps.approval_hook = display.approve_action  # 3-way gate: confirm + activation
    try:
        if args.task:
            from .engine import guardrails

            allowed, text = guardrails.check_input(config.settings, " ".join(args.task))
            if not allowed:
                display.warn(text)
                return 0
            prompt = build_user_prompt(
                text, args.image or [], allow_local=True,
                max_mb=max_mb_from(config.settings),
            )
            return _one_shot(agent, prompt, deps, config.model)
        return _repl(agent, config, deps, session_id=args.session)
    finally:
        close_deps(deps)


def _run_gateway(config, name: str) -> int:
    """Run one messaging gateway in this process until interrupted (Phase 22).

    This is the target of ``agent --gateway <name>`` — normally spawned as a
    subprocess by the menu's gateway manager (in its own console window), but
    runnable by hand too. On a console it renders a live monitor (banner + feed +
    closing stats); headless it logs plainly. Either way it always appends to the
    gateway's log file so the menu's "View log" works.
    """
    from .gateways import get_gateway, manager

    deps = build_deps(config)
    try:
        try:
            gateway = get_gateway(config, name, deps)
        except KeyError as exc:
            display.err(str(exc))
            return 2
        problem = gateway.validate()
        if problem:
            display.err(f"cannot start gateway '{name}': {problem}")
            return 2

        # Logging: always persist to the gateway log file; on a console show the
        # rich monitor instead of raw log lines (so the window is a live feed).
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        file_handler = logging.FileHandler(manager.log_path(config, name), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(file_handler)
        root.setLevel(logging.INFO)
        # httpx logs one INFO line per long-poll (~every 25s) — megabytes/month of
        # noise that drowns "View log". Keep only its warnings/errors.
        logging.getLogger("httpx").setLevel(logging.WARNING)

        monitor = None
        if sys.stdout.isatty():
            monitor = display.GatewayMonitor(name, gateway.status_info())
            gateway.monitor = monitor
            monitor.on_start()
        else:
            root.addHandler(logging.StreamHandler())   # headless: also to stdout

        try:
            asyncio.run(gateway.run())
        except KeyboardInterrupt:
            pass
        if monitor is not None:
            monitor.print_stats()
        return 0
    finally:
        close_deps(deps)


def _one_shot(agent, task, deps, model: str) -> int:
    try:
        result = asyncio.run(display.run_streamed(agent, task, deps, model))
    except KeyboardInterrupt:
        display.warn("interrupted")
        return 130
    except UsageLimitExceeded as exc:
        display.warn(f"usage limit reached — {exc}")
        return 0
    except Exception as exc:  # noqa: BLE001 - a model/provider failure, not a crash
        display.err(f"model error: {exc}{vision_hint(exc)}")
        return 1
    display.answer(result.output, markdown=deps.settings.get("render_markdown", True))
    return 0


def _attach_name(val) -> str:
    """Display name for a pending attachment ((name, body) text doc, or a path)."""
    return val[0] if isinstance(val, tuple) else Path(val).name


def _drain_cli_deliveries(deps) -> None:
    """Show scheduled-task results delivered to the CLI (Phase 23g).

    The REPL is not a runner — it only surfaces results produced by a gateway/the
    server (which share this store). Printed between prompts, once each.
    """
    if not (deps.settings.get("scheduler") or {}).get("enabled"):
        return
    from .runtime import scheduler

    try:
        records = scheduler.pending_for(deps.store, "cli")
    except Exception:  # noqa: BLE001 - delivery is best-effort, never break the REPL
        return
    for rec in records:
        display.info(f"scheduled result [{rec.get('job_id')}]:")
        display.answer(rec["text"], markdown=deps.settings.get("render_markdown", True))
        scheduler.mark_delivered(deps.store, rec["id"], "cli")


def _repl(agent, config, deps, session_id=None) -> int:
    from .runtime import threads

    tools = discover_tools(config)
    display.print_banner(config, tools)
    # Conversation memory: the running transcript fed back on each turn so the
    # REPL is a conversation, not amnesia. Capped to the last `history_keep`
    # messages (context is finite, and UsageLimits would otherwise start failing
    # long sessions). One-shot and the server stay stateless by design.
    history: list = []
    keep = int(config.settings.get("history_keep", 40))

    # Persistent threads (Phase 18, opt-in): when threads.enabled and a session
    # is active, the conversation is loaded at start and saved back each turn, so
    # it survives a restart. `session` is None = ephemeral (today's behaviour).
    threads_on = threads.enabled(config.settings)
    session = session_id if threads_on else None
    if session_id and not threads_on:
        display.warn("--session ignored: set threads.enabled: true in settings.yaml")
    if session:
        history = threads.load_thread(deps.store, session)
        if history:
            display.info(f"resumed thread '{session}' ({len(history)} messages)")
        else:
            display.info(f"new thread '{session}'")

    # prompt_toolkit input: correct multi-line paste, ↑/↓ history, line editing
    # (cross-platform). Falls back to input() when unavailable / not a TTY.
    pt = display.new_prompt_session(deps.workspace)
    deps.extra["channel_origin"] = {"channel": "cli"}   # for schedule_task (Phase 23)
    return _repl_loop(agent, config, deps, tools, history, keep, threads_on, session, pt)


def _repl_loop(agent, config, deps, tools, history, keep, threads_on, session, pt) -> int:
    from .runtime import threads
    from .runtime.attachments import classify_attachment, inline_text_docs

    pending: list[tuple[str, object]] = []   # /attach'd files for the next message
    while True:
        _drain_cli_deliveries(deps)           # surface scheduled results (Phase 23g)
        try:
            task = display.read_line(pt, "  \033[1m›\033[0m ").strip()
        except KeyboardInterrupt:            # Ctrl+C cancels the current line
            continue
        except EOFError:                     # Ctrl+D exits
            print()
            break
        if not task:
            continue
        if task in ("/quit", "/exit", "/q"):
            break
        if task == "/help":
            display.info(
                "Type a task. Commands: /help · /tools · /clear · /reload · "
                "/attach <path> · /threads · /resume <id> · /new · /quit"
            )
            display.info(
                "Attach files: /attach <path> (images/PDF for vision models; text "
                "docs are inlined) — or just drag a file into the terminal."
            )
            continue
        if task.startswith("/attach"):
            arg = task[len("/attach"):].strip()
            if not arg:
                if pending:
                    names = ", ".join(_attach_name(v) for _, v in pending)
                    display.info(f"pending attachments: {names}")
                else:
                    display.info("usage: /attach <path>  (file to send with your next message)")
                continue
            kind, val = classify_attachment(arg, max_mb_from(config.settings))
            if kind == "error":
                display.warn(f"attach failed: {val}")
            else:
                pending.append((kind, val))
                display.info(f"attached: {_attach_name(val)}  ({len(pending)} pending)")
            continue
        if task == "/clear":
            history.clear()
            display.info("conversation history cleared")
            continue
        if task == "/threads":
            if not threads_on:
                display.info("threads are off — set threads.enabled: true in settings.yaml")
            else:
                rows = threads.sessions_by_recency(deps.store)
                if not rows:
                    display.info("no saved threads yet")
                else:
                    for r in rows:
                        marker = "* " if r["id"] == session else "  "
                        title = r.get("title") or "(untitled)"
                        when = display.relative_time(r.get("updated_at"))
                        display.info(f"{marker}{r['id']}  ·  {title}  ·  {when}  ·  {r.get('channel') or '—'}")
                    if session:
                        display.info(f"current thread: {session}")
            continue
        if task.startswith("/resume"):
            if not threads_on:
                display.info("threads are off — set threads.enabled: true in settings.yaml")
                continue
            parts = task.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                display.warn("usage: /resume <id>")
                continue
            session = parts[1].strip()
            history = threads.load_thread(deps.store, session)
            display.info(f"resumed thread '{session}' ({len(history)} messages)")
            continue
        if task == "/new":
            session = None
            history.clear()
            display.info("started a fresh conversation (not persisted — /resume <id> to name it)")
            continue
        if task in ("/reload", "/tools"):
            from .engine.registry import tool_names

            if task == "/reload":
                # Rebuild the agent so newly approved tools (Phase 11) register.
                agent = build_agent(config)
                tools = discover_tools(config)
                display.ok("tools reloaded")
            display.info("tools: " + ", ".join(tool_names(tools)))
            continue
        # Input guardrails (Phase 21): refuse or redact before anything runs.
        from .engine import guardrails

        allowed, task = guardrails.check_input(config.settings, task)
        if not allowed:
            display.warn(task)
            continue
        # Attachments: files dropped into the line (collapsed to a chip, expanded
        # back here) + any /attach'd this turn. Each is classified: image/PDF →
        # multimodal part (vision), text doc → inlined into the prompt.
        clean, dragged = extract_attachments(task)
        media: list[str] = []
        docs: list[tuple[str, str]] = []
        for kind, val in pending:
            (media.append(val) if kind == "media" else docs.append(val))
        pending.clear()
        for p in dragged:
            kind, val = classify_attachment(p, max_mb_from(config.settings))
            if kind == "media":
                media.append(val)
            elif kind == "text":
                docs.append(val)
        if dragged:
            display.info("attached: " + ", ".join(Path(a).name for a in dragged))
        base = clean if dragged else task
        task_text = inline_text_docs(base, docs)
        if media:
            prompt = build_user_prompt(
                task_text or "(see attached)", media, allow_local=True,
                max_mb=max_mb_from(config.settings),
            )
        else:
            prompt = task_text
        # The whole turn runs under ONE `async with agent:` so the auto-title call
        # (a side model_request after the run) still has an open provider client:
        # `run_streamed` opens its own `async with agent:`, and pydantic_ai closes
        # the model's HTTP client when that block exits — so titling *after* it
        # would hit a closed client ("Connection error"). Wrapping here keeps the
        # client open via pydantic_ai's enter/exit ref-count until the turn ends.
        async def _turn():
            async with agent:
                result = await display.run_streamed(
                    agent, prompt, deps, config.model, message_history=history
                )
                display.answer(result.output, markdown=config.settings.get("render_markdown", True))
                history.extend(result.new_messages())
                if len(history) > keep:
                    del history[:-keep]
                if session:                   # persist + auto-title the thread (Phase 18/37)
                    threads.save_thread(deps.store, session, history, keep=keep, channel="cli")
                    await threads.autotitle_thread(
                        deps.store, session, history, config.settings,
                        model=agent.model, usage=threads.usage_of(result),
                    )
                return result

        try:
            asyncio.run(_turn())            # renders the answer + persists inside _turn
            # A tool the agent authored + got approved this turn → hot-reload so
            # it's callable immediately (Phase 11b).
            if deps.extra.pop("reload_pending", False):
                agent = build_agent(config)
                tools = discover_tools(config)
                display.ok("new tool activated — toolset reloaded")
        except KeyboardInterrupt:
            display.warn("interrupted")
        except UsageLimitExceeded as exc:
            display.warn(f"usage limit reached — {exc}")
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive
            display.err(f"{exc}{vision_hint(exc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
