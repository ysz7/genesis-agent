"""Phase 29: extended thinking / reasoning budget — the model-settings seam.

Live reasoning needs a provider key (external resource); here we assert the
settings→ModelSettings mapping per provider, the temperature-drop guard for
Anthropic, and that the runner surfaces thinking as a distinct event.
"""

from pathlib import Path

import pytest

from agent.runtime.config import Config
from agent.engine.factory import build_agent
from agent.engine.model import thinking_model_settings


def _cfg(provider: str, thinking: dict | None = None, **settings) -> Config:
    s: dict = dict(settings)
    if thinking is not None:
        s["thinking"] = thinking
    return Config(
        root=Path("."),
        provider=provider,
        model="test-model",
        api_key="sk-test",
        base_url=None,
        persona="x",
        settings=s,
    )


# ── The mapping seam ─────────────────────────────────────────────────────────

def test_thinking_off_by_default():
    assert thinking_model_settings(_cfg("anthropic")) == {}


def test_disabled_block_is_noop():
    assert thinking_model_settings(_cfg("anthropic", {"enabled": False, "effort": "high"})) == {}


def test_non_dict_block_is_noop():
    assert thinking_model_settings(_cfg("anthropic", thinking="yes")) == {}  # type: ignore[arg-type]


def test_anthropic_effort_uses_portable_key():
    got = thinking_model_settings(_cfg("anthropic", {"enabled": True, "effort": "high"}))
    assert got == {"thinking": "high"}


def test_anthropic_budget_uses_provider_key():
    got = thinking_model_settings(
        _cfg("anthropic", {"enabled": True, "effort": "high", "budget_tokens": 4000})
    )
    assert got == {"anthropic_thinking": {"type": "enabled", "budget_tokens": 4000}}


def test_openai_effort_uses_portable_key():
    got = thinking_model_settings(_cfg("openai", {"enabled": True, "effort": "medium"}))
    assert got == {"thinking": "medium"}


def test_openai_budget_is_ignored_uses_effort():
    # budget_tokens is Anthropic-only; OpenAI still rides the portable key.
    got = thinking_model_settings(
        _cfg("openai", {"enabled": True, "effort": "low", "budget_tokens": 9999})
    )
    assert got == {"thinking": "low"}


def test_default_effort_when_unset():
    assert thinking_model_settings(_cfg("openai", {"enabled": True})) == {"thinking": True}


def test_reasoning_effort_alias():
    got = thinking_model_settings(_cfg("openai", {"enabled": True, "reasoning_effort": "high"}))
    assert got == {"thinking": "high"}


@pytest.mark.parametrize("provider", ["openrouter", "ollama"])
def test_other_providers_are_noop(provider):
    assert thinking_model_settings(_cfg(provider, {"enabled": True, "effort": "high"})) == {}


# ── The temperature-drop guard (factory) ─────────────────────────────────────

def test_anthropic_thinking_drops_temperature():
    cfg = _cfg("anthropic", {"enabled": True, "effort": "high"})
    cfg.model_settings = {"temperature": 0, "top_p": 0.5, "timeout": 60}
    agent = build_agent(cfg)
    ms = agent.model_settings or {}
    assert "temperature" not in ms and "top_p" not in ms  # both dropped
    assert ms.get("timeout") == 60  # unrelated knobs survive
    assert ms.get("thinking") == "high"


def test_openai_thinking_keeps_temperature():
    cfg = _cfg("openai", {"enabled": True, "effort": "high"})
    cfg.model_settings = {"temperature": 0, "timeout": 60}
    agent = build_agent(cfg)
    ms = agent.model_settings or {}
    assert ms.get("temperature") == 0  # OpenAI reasoning tolerates temperature
    assert ms.get("thinking") == "high"


def test_thinking_off_leaves_temperature():
    cfg = _cfg("anthropic")
    cfg.model_settings = {"temperature": 0}
    agent = build_agent(cfg)
    assert (agent.model_settings or {}).get("temperature") == 0


# ── The runner surfaces thinking distinctly ──────────────────────────────────

def test_think_event_is_distinct_from_reason():
    from agent.engine.runner import Reason, Think

    assert Think("x").text == "x"
    assert Think is not Reason  # separate types → renderers can tell them apart
