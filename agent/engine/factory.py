"""The single composition root: wire model + persona + tools into an Agent.

Everything else (entrypoints, server, console) calls :func:`build_agent`. This
is the only function that constructs the Pydantic AI ``Agent``; swap any leaf
(model via ``.env``, tools via a dropped file, persona via ``persona.md``)
without changing this signature.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from datetime import datetime
from typing import Any

from pydantic_ai import Agent, RunContext

from ..runtime.config import Config
from ..runtime.context import AgentDeps
from .compaction import build_history_processor
from .mcp import load_mcp_servers
from .model import build_model, cache_model_settings, thinking_model_settings
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
    persona_override: str | None = None,
    model_override: str | None = None,
) -> Agent:
    """Compose ``Agent(model, system_prompt, deps_type, output_type, tools)``.

    Args:
        config: A loaded :class:`Config`.
        output_type: Optional Pydantic model for structured output. When ``None``
            the agent returns plain text.
        exclude_tools: Tool names to leave out of this build — used to construct
            a restricted sub-agent (Phase 14 delegation).
        persona_override: Replace ``config.persona`` as the static system prompt —
            used to build a *named* sub-agent with its own persona (Phase 17).
        model_override: Swap the model id (same provider/key) — lets a named
            sub-agent run on a cheaper/stronger model (Phase 17f).
    """
    _setup_observability()
    if model_override:
        config = dataclasses.replace(config, model=model_override)
    model = build_model(config)
    tools = discover_tools(config, exclude=exclude_tools)

    kwargs: dict[str, Any] = {
        "system_prompt": persona_override or config.persona,
        "deps_type": AgentDeps,
        "tools": tools,
        "retries": int(config.settings.get("retries", 2)),
    }
    if output_type is not None:
        kwargs["output_type"] = output_type
    # Model knobs (temperature, max_tokens, timeout, …) passed through as-is so
    # new Pydantic AI ModelSettings keys work without changing the template,
    # merged with any provider prompt-caching settings (Phase 16, opt-in) and the
    # extended-thinking / reasoning-budget knob (Phase 29, opt-in).
    model_settings = {**(config.model_settings or {}), **cache_model_settings(config)}
    thinking = thinking_model_settings(config)
    if thinking:
        model_settings.update(thinking)
        # Anthropic extended thinking rejects a fixed temperature/top_p (it needs
        # them unset, =1). The template ships temperature: 0, so drop the conflict
        # rather than let enabling thinking error the run.
        if config.provider == "anthropic":
            for knob in ("temperature", "top_p"):
                if model_settings.pop(knob, None) is not None:
                    logger.info("thinking on Anthropic: dropped %s (must be unset)", knob)
    if model_settings:
        kwargs["model_settings"] = model_settings

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

    # Subagent roster (Phase 17): surface the named subagents in workspace/agents/
    # so the top agent knows who it can delegate_to. Depth-aware — a sub-agent
    # (delegation_depth > 0) is shown nothing, so the roster doesn't recurse.
    if (config.settings.get("subagents") or {}).get("enabled"):
        from ..tools.subagents import agents_overview

        workspace = config.workspace

        @agent.system_prompt
        def _subagent_roster(ctx: RunContext[AgentDeps]) -> str:
            if int(ctx.deps.extra.get("delegation_depth", 0)) > 0:
                return ""
            return agents_overview(workspace)

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
        def _memory(ctx: RunContext[AgentDeps]) -> str:
            # Semantic recall (Phase 19): rank lessons by relevance to the current
            # task when memory.semantic is on; else recency (Phase 11f).
            from ..runtime import memory

            if memory.semantic_enabled(config.settings):
                return memory.semantic_recall(ctx.deps, memory.current_query(ctx), recall)
            return memory_digest(workspace, recall)

    # Output guardrails (Phase 21, opt-in): a content validator that redacts or
    # rejects (→ retry) the model's answer per `guardrails.output` in settings.
    from .guardrails import output_validator_for

    validator = output_validator_for(config.settings)
    if validator is not None:
        agent.output_validator(validator)

    # Secret redaction (Phase 27a): scrub .env values from the final answer —
    # the belt to the registry wrapper's braces (covers a model echoing a secret
    # it saw before redaction was enabled, or one pasted into the task itself).
    from ..runtime import secrets as _secrets

    if _secrets.enabled(config.settings):
        secret_values = _secrets.collect_secrets(config.root)
        if secret_values:
            @agent.output_validator
            def _redact_answer(value):
                return _secrets.redact_value(value, secret_values)

    return agent
