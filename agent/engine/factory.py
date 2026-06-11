"""The single composition root: wire model + persona + tools into an Agent.

Everything else (entrypoints, server, console) calls :func:`build_agent`. This
is the only function that constructs the Pydantic AI ``Agent``; swap any leaf
(model via ``.env``, tools via a dropped file, persona via ``persona.md``)
without changing this signature.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic_ai import Agent

from ..runtime.config import Config
from ..runtime.context import AgentDeps
from .compaction import build_history_processor
from .mcp import load_mcp_servers
from .model import build_model
from .registry import discover_tools


def build_agent(config: Config, output_type: Any | None = None) -> Agent:
    """Compose ``Agent(model, system_prompt, deps_type, output_type, tools)``.

    Args:
        config: A loaded :class:`Config`.
        output_type: Optional Pydantic model for structured output. When ``None``
            the agent returns plain text.
    """
    model = build_model(config)
    tools = discover_tools(config)

    kwargs: dict[str, Any] = {
        "system_prompt": config.persona,
        "deps_type": AgentDeps,
        "tools": tools,
        "retries": int(config.settings.get("retries", 2)),
    }
    if output_type is not None:
        kwargs["output_type"] = output_type
    # Model knobs (temperature, max_tokens, timeout, …) passed through as-is so
    # new Pydantic AI ModelSettings keys work without changing the template.
    if config.model_settings:
        kwargs["model_settings"] = config.model_settings

    # History auto-compaction: when the conversation outgrows the context
    # budget, old messages are replaced by a model-written summary (see
    # engine/compaction.py). Disabled via settings `compaction: {enabled: false}`.
    processor = build_history_processor(config, model)
    if processor is not None:
        from pydantic_ai.capabilities import ProcessHistory

        kwargs["capabilities"] = [ProcessHistory(processor)]

    mcp_servers = load_mcp_servers(config)
    if mcp_servers:
        kwargs["toolsets"] = mcp_servers

    agent = Agent(model, **kwargs)

    # A dynamic system prompt, re-evaluated on every run, so the model always
    # knows the current local date/time (useful for "today", scheduled jobs, etc.).
    @agent.system_prompt
    def _current_datetime() -> str:
        now = datetime.now().astimezone()
        return f"The current date and time is {now:%Y-%m-%d %H:%M:%S %Z} ({now:%A})."

    return agent
