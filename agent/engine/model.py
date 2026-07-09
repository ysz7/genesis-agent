"""``PROVIDER``/``MODEL``/``BASE_URL`` → a Pydantic AI ``Model``.

This is the ONLY place a provider is chosen. OpenRouter, Ollama, and any other
OpenAI-compatible endpoint ride the OpenAI provider with a ``base_url``; only
Anthropic gets its own provider. Adding a provider here is the entire cost of
supporting it — every tool, the console, and the server are provider-agnostic.
"""

from __future__ import annotations

import logging

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from ..runtime.config import Config

logger = logging.getLogger("agent.model")


def build_model(config: Config) -> Model:
    """Map a loaded :class:`Config` to a concrete Pydantic AI ``Model``.

    With ``model_fallbacks: [id, ...]`` set in ``settings.yaml`` (Phase 20), the
    primary model is wrapped in a Pydantic AI ``FallbackModel``: a transient
    provider failure (HTTP error / rate-limit / outage) transparently retries the
    next model id, same provider/key. Without it, the bare primary is returned —
    behaviour is byte-identical to before.
    """
    primary = _build_one(config, config.model)

    fallbacks = config.settings.get("model_fallbacks") or []
    if not fallbacks:
        return primary

    from pydantic_ai.models.fallback import FallbackModel

    models = [primary] + [_build_one(config, str(m)) for m in fallbacks]
    return FallbackModel(*models)


def _build_one(config: Config, model_id: str) -> Model:
    """Build a single concrete model for *model_id* on the configured provider."""
    provider = config.provider

    if provider == "anthropic":
        return AnthropicModel(
            model_id,
            provider=AnthropicProvider(api_key=config.api_key or ""),
        )

    # openai · openrouter · ollama · any OpenAI-compatible endpoint
    if provider in ("openai", "openrouter", "ollama") or config.base_url:
        kwargs: dict = {"api_key": config.api_key or "not-needed"}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return OpenAIChatModel(model_id, provider=OpenAIProvider(**kwargs))

    raise ValueError(
        f"Unknown PROVIDER={provider!r}. Use one of: "
        "openai, anthropic, openrouter, ollama (or set BASE_URL for a "
        "custom OpenAI-compatible endpoint)."
    )


def cache_model_settings(config: Config) -> dict:
    """Provider-specific prompt-caching model settings (empty unless enabled).

    Enabled by ``prompt_caching: true``. For Anthropic we cache the **tool
    definitions** — a large, fully static prefix that's identical every run, so
    the second run within the cache TTL reads it instead of re-billing it.

    We deliberately do NOT cache the system instructions: genesis injects a
    dynamic datetime (and optionally skills/memory/plan) system prompt, and
    Anthropic puts the cache breakpoint on the *last* system block — so a
    changing system prompt would be written to cache every run but never read.
    OpenAI caches automatically (nothing to set); other providers: no-op.
    """
    if not config.settings.get("prompt_caching"):
        return {}
    if config.provider == "anthropic":
        return {"anthropic_cache_tool_definitions": True}
    return {}


def thinking_model_settings(config: Config) -> dict:
    """Extended-thinking / reasoning-budget model settings (empty unless enabled).

    Opt-in via ``thinking: {enabled: true, effort: high, budget_tokens: N}`` in
    ``settings.yaml`` (off by default; inert without the block — same lean-by-
    default posture as caching/MCP). ``effort`` (``minimal|low|medium|high|xhigh``,
    with ``reasoning_effort`` accepted as an alias) maps to Pydantic AI's
    provider-agnostic ``thinking`` ``ModelSettings`` key, so it works on any
    reasoning-capable model. For Anthropic, an optional ``budget_tokens`` uses the
    provider-specific ``anthropic_thinking`` for exact token control.

    Active for ``anthropic`` and ``openai`` (the providers with a portable
    reasoning knob here). ``openrouter``/``ollama``/custom endpoints degrade to a
    no-op with a one-line hint — reasoning there is model-specific, so a vertical
    sets ``model_settings.thinking`` by hand rather than risk an API error.
    """
    block = config.settings.get("thinking")
    block = block if isinstance(block, dict) else {}
    if not block.get("enabled"):
        return {}

    effort = block.get("effort") or block.get("reasoning_effort")
    budget = block.get("budget_tokens")

    if config.provider == "anthropic":
        if budget:
            return {"anthropic_thinking": {"type": "enabled", "budget_tokens": int(budget)}}
        return {"thinking": str(effort) if effort else True}
    if config.provider == "openai":
        return {"thinking": str(effort) if effort else True}

    logger.info(
        "thinking is enabled but provider %r has no portable reasoning knob here — "
        "no-op (set model_settings.thinking by hand if your model supports it)",
        config.provider,
    )
    return {}
