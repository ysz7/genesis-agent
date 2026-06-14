"""The single composition root: wire model + persona + tools into an Agent.

Everything else (entrypoints, server, console) calls :func:`build_agent`. This
is the only function that constructs the Pydantic AI ``Agent``; swap any leaf
(model via ``.env``, tools via a dropped file, persona via ``persona.md``)
without changing this signature.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from pydantic_ai import Agent, RunContext

from ..runtime.config import Config
from ..runtime.context import AgentDeps
from .compaction import build_history_processor
from .mcp import load_mcp_servers
from .model import build_model
from .registry import discover_tools

logger = logging.getLogger("agent.obs")

_observability_done = False


def _setup_observability() -> None:
    """Opt-in Logfire tracing — lean by default, same pattern as MCP.

    Active only when ``LOGFIRE_TOKEN`` is set in ``.env`` AND the optional
    ``logfire`` package is installed (``uv sync --extra obs``); otherwise it
    degrades silently with a debug-level hint. Configured once per process.
    """
    global _observability_done
    if _observability_done:
        return
    _observability_done = True
    if not os.getenv("LOGFIRE_TOKEN"):
        return
    try:
        import logfire
    except ImportError:
        logger.debug(
            "LOGFIRE_TOKEN is set but logfire isn't installed — uv sync --extra obs"
        )
        return
    logfire.configure()
    logfire.instrument_pydantic_ai()
    logger.info("logfire tracing enabled")


def build_agent(
    config: Config,
    output_type: Any | None = None,
    *,
    exclude_tools: set[str] | None = None,
) -> Agent:
    """Compose ``Agent(model, system_prompt, deps_type, output_type, tools)``.

    Args:
        config: A loaded :class:`Config`.
        output_type: Optional Pydantic model for structured output. When ``None``
            the agent returns plain text.
        exclude_tools: Tool names to leave out of this build — used to construct
            a restricted sub-agent (Phase 14 delegation).
    """
    _setup_observability()
    model = build_model(config)
    tools = discover_tools(config, exclude=exclude_tools)

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

    # Explicit planning (Phase 13): surface the current todo checklist so the
    # model tracks its own progress across turns. Reads per-run state from deps.
    if (config.settings.get("planning") or {}).get("enabled"):
        from ..tools.planning import plan_overview

        @agent.system_prompt
        def _plan(ctx: RunContext[AgentDeps]) -> str:
            return plan_overview(ctx.deps)

    # Self-improvement context (Phase 11): surface the skill index and a digest
    # of recent lessons so the model knows what it has already learned/written.
    # Full skill text stays on disk (pulled via read_skill) to keep context lean.
    if (config.settings.get("self_improvement") or {}).get("enabled"):
        from ..tools.selfimprove import memory_digest, skills_overview

        recall = int(config.settings.get("memory_recall", 5))
        workspace = config.workspace

        @agent.system_prompt
        def _skills_index() -> str:
            return skills_overview(workspace)

        @agent.system_prompt
        def _memory() -> str:
            return memory_digest(workspace, recall)

    return agent
