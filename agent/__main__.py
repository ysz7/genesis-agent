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
import sys

from .console import display
from .runtime.config import load_config
from .runtime.context import build_deps, close_deps
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
    deps.confirm_hook = display.confirm_tool  # interactive y/N for confirm-gated tools
    try:
        if args.task:
            return _one_shot(agent, " ".join(args.task), deps, config.model)
        return _repl(agent, config, deps)
    finally:
        close_deps(deps)


def _one_shot(agent, task: str, deps, model: str) -> int:
    try:
        result = asyncio.run(display.run_streamed(agent, task, deps, model))
    except KeyboardInterrupt:
        display.warn("interrupted")
        return 130
    display.answer(result.output)
    return 0


def _repl(agent, config, deps) -> int:
    tools = discover_tools(config)
    display.print_banner(config, tools)
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
            display.info("Type a task. Commands: /help · /tools · /quit")
            continue
        if task == "/tools":
            from .engine.registry import tool_names

            display.info("tools: " + ", ".join(tool_names(tools)))
            continue
        try:
            result = asyncio.run(display.run_streamed(agent, task, deps, config.model))
            display.answer(result.output)
        except KeyboardInterrupt:
            display.warn("interrupted")
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive
            display.err(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
