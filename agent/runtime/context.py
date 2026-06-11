"""``AgentDeps`` — the typed dependency bundle injected into every tool run.

This is the single place a vertical adds a shared client (broker, API, cache)
without changing any tool signature. Tools reach it via Pydantic AI's
``RunContext[AgentDeps]`` first parameter::

    def my_tool(ctx: RunContext[AgentDeps], symbol: str) -> str:
        price = ctx.deps.http.get(...)          # shared http client
        ctx.deps.store.set("last", symbol)      # cross-run state
        feeds = ctx.deps.settings["feeds"]      # settings.yaml

To add a vertical client, add a field here and set it in :func:`build_deps`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Config
from .store import Store, open_store


@dataclass
class AgentDeps:
    """Shared dependencies available to every tool through ``ctx.deps``."""

    config: Config
    settings: dict
    store: Store
    http: httpx.Client
    workspace: Path
    # Confirmation gate for ``tools.confirm`` (set by the CLI to a y/N prompt).
    # ``(tool_name, rendered_args) -> approve?``. ``None`` means no human is
    # available (headless/server): a confirm-listed tool then refuses to run.
    confirm_hook: Callable[[str, str], bool] | None = None
    # Verticals add their own clients here (e.g. ``broker: BrokerClient``)
    extra: dict[str, Any] = field(default_factory=dict)


def build_deps(config: Config) -> AgentDeps:
    """Construct the dependency bundle from a loaded :class:`Config`.

    Opens the state store (``workspace/state.<ext>``, JSON by default) and a
    shared HTTP client. Call :func:`close_deps` when done to release them.
    """
    store_name = str(config.settings.get("store", "state.json"))
    store = open_store(config.workspace / store_name)
    http = httpx.Client(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
        headers={"User-Agent": f"genesis-agent/{config.agent_name}"},
    )
    return AgentDeps(
        config=config,
        settings=config.settings,
        store=store,
        http=http,
        workspace=config.workspace,
    )


def close_deps(deps: AgentDeps) -> None:
    """Release resources held by the deps bundle."""
    try:
        deps.http.close()
    finally:
        deps.store.close()
