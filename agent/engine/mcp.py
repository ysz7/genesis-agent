"""Optional MCP seam — plug external Model Context Protocol servers in as tools.

Opt-in and lean-by-default: nothing here runs unless ``settings.yaml`` declares an
``mcp:`` list AND the optional ``mcp`` dependency is installed
(``uv sync --extra mcp``). Pydantic AI exposes MCP servers as *toolsets*, which
:func:`agent.engine.factory.build_agent` passes straight to the ``Agent``; their tools
then appear to the model exactly like built-in tools.

settings.yaml example::

    mcp:
      - name: filesystem                       # → tool name prefix (optional)
        command: npx                           # stdio server (spawned locally)
        args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      - name: docs
        url: https://example.com/mcp           # remote streamable-HTTP server
        headers:
          Authorization: "Bearer ..."

Each entry is either a **stdio** server (`command` + `args`) or a **remote**
server (`url`). Runs that use MCP must enter the agent context so the servers
start/stop — the CLI and server already do this via ``async with agent:``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..runtime.config import Config

logger = logging.getLogger("agent.mcp")


def load_mcp_servers(config: Config) -> list[Any]:
    """Build MCP server toolsets from ``settings.yaml``'s ``mcp:`` list.

    Returns ``[]`` when none are configured, or when the optional ``mcp``
    dependency is missing (with a one-line hint) — so the agent still runs.
    """
    entries = config.settings.get("mcp") or []
    if not entries:
        return []

    try:
        from pydantic_ai.mcp import MCPServerStdio, MCPServerStreamableHTTP
    except Exception:
        logger.warning(
            "settings.yaml declares mcp servers but the 'mcp' extra isn't "
            "installed — run: uv sync --extra mcp"
        )
        return []

    servers: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        prefix = entry.get("name")
        if entry.get("url"):
            servers.append(
                MCPServerStreamableHTTP(
                    entry["url"],
                    headers=entry.get("headers"),
                    tool_prefix=prefix,
                )
            )
        elif entry.get("command"):
            servers.append(
                MCPServerStdio(
                    entry["command"],
                    args=entry.get("args", []),
                    env=entry.get("env"),
                    cwd=entry.get("cwd"),
                    tool_prefix=prefix,
                )
            )
    return servers
