"""genesis-agent — public API surface for tool authors.

Tools you drop in ``tools/*.py`` reach shared deps via ``RunContext[AgentDeps]``::

    from pydantic_ai import RunContext
    from agent import AgentDeps

    def my_tool(ctx: RunContext[AgentDeps], x: str) -> str:
        ...

The reusable helpers from the toolkit are re-exported here too. Engine internals
live in subpackages (``agent.runtime``, ``agent.engine``, ``agent.console``,
``agent.server``) and are not part of the stable surface.
"""

from .runtime.config import Config
from .runtime.context import AgentDeps
from .tools.toolkit import FeedItem, TTLCache, http_get, parse_rss

__all__ = ["AgentDeps", "Config", "FeedItem", "TTLCache", "http_get", "parse_rss"]
