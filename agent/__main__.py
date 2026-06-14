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
    parser.add_argument("--port", type=int, default=8181, help="port for --serve")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind address for --serve (default localhost; use 0.0.0.0 in containers)",
    )
    parser.add_argument("--root", default=None, help="agent folder (default: cwd)")
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
    # --serve stays on plain stdlib logging (server code never imports rich).
    if args.serve:
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

    agent = build_agent(config)
    deps = build_deps(config)
    deps.approval_hook = display.approve_action  # 3-way gate: confirm + activation
    try:
        if args.task:
            prompt = build_user_prompt(
                " ".join(args.task), args.image or [], allow_local=True,
                max_mb=max_mb_from(config.settings),
            )
            return _one_shot(agent, prompt, deps, config.model)
        return _repl(agent, config, deps)
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
    display.answer(result.output)
    return 0


def _repl(agent, config, deps) -> int:
    tools = discover_tools(config)
    display.print_banner(config, tools)
    # Conversation memory: the running transcript fed back on each turn so the
    # REPL is a conversation, not amnesia. Capped to the last `history_keep`
    # messages (context is finite, and UsageLimits would otherwise start failing
    # long sessions). One-shot and the server stay stateless by design.
    history: list = []
    keep = int(config.settings.get("history_keep", 40))
    while True:
        try:
            task = input("  \033[1m›\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not task:
            continue
        if task in ("/quit", "/exit", "/q"):
            break
        if task == "/help":
            display.info("Type a task. Commands: /help · /tools · /clear · /reload · /quit")
            continue
        if task == "/clear":
            history.clear()
            display.info("conversation history cleared")
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
        # Drag a file into the terminal → it arrives as a path; attach it.
        clean, attached = extract_attachments(task)
        if attached:
            display.info("📎 attached: " + ", ".join(Path(a).name for a in attached))
            prompt = build_user_prompt(
                clean or "(see attached)", attached, allow_local=True,
                max_mb=max_mb_from(config.settings),
            )
        else:
            prompt = task
        try:
            result = asyncio.run(
                display.run_streamed(agent, prompt, deps, config.model, message_history=history)
            )
            display.answer(result.output)
            history.extend(result.new_messages())
            if len(history) > keep:
                del history[:-keep]
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
