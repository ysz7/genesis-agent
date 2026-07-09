"""Phase 16: prompt caching — the provider seam (offline).

Live cache-read verification needs an Anthropic key (external resource); here we
assert the model-settings seam picks the right keys per provider.
"""

from agent.runtime.config import load_config
from agent.engine.model import cache_model_settings


def test_caching_off_by_default(tmp_path):
    assert cache_model_settings(load_config(tmp_path)) == {}


def test_anthropic_caches_tool_definitions(tmp_path):
    cfg = load_config(tmp_path)
    cfg.settings["prompt_caching"] = True
    cfg.provider = "anthropic"
    assert cache_model_settings(cfg) == {"anthropic_cache_tool_definitions": True}


def test_other_providers_are_noop(tmp_path):
    cfg = load_config(tmp_path)
    cfg.settings["prompt_caching"] = True
    for provider in ("openai", "openrouter", "ollama"):
        cfg.provider = provider
        assert cache_model_settings(cfg) == {}  # OpenAI auto-caches; others: nothing


def test_disabled_is_noop_even_on_anthropic(tmp_path):
    cfg = load_config(tmp_path)
    cfg.provider = "anthropic"
    cfg.settings["prompt_caching"] = False
    assert cache_model_settings(cfg) == {}


# ── Phase 34: prefix caching over the conversation history ────────────────────

def test_prefix_form_caches_tools_and_prefix(tmp_path):
    cfg = load_config(tmp_path)
    cfg.provider = "anthropic"
    cfg.settings["prompt_caching"] = {"tools": True, "prefix": True}
    assert cache_model_settings(cfg) == {
        "anthropic_cache_tool_definitions": True,
        "anthropic_cache": True,  # the moving prefix breakpoint
    }


def test_prefix_defaults_tools_on(tmp_path):
    """Enabling only `prefix` still keeps tool-defs caching (tools defaults true)."""
    cfg = load_config(tmp_path)
    cfg.provider = "anthropic"
    cfg.settings["prompt_caching"] = {"prefix": True}
    assert cache_model_settings(cfg) == {
        "anthropic_cache_tool_definitions": True,
        "anthropic_cache": True,
    }


def test_prefix_can_be_tools_only(tmp_path):
    """Object form with tools off, prefix off matches nothing (explicit opt-out)."""
    cfg = load_config(tmp_path)
    cfg.provider = "anthropic"
    cfg.settings["prompt_caching"] = {"tools": False, "prefix": False}
    assert cache_model_settings(cfg) == {}


def test_prefix_form_noop_on_other_providers(tmp_path):
    cfg = load_config(tmp_path)
    cfg.settings["prompt_caching"] = {"tools": True, "prefix": True}
    for provider in ("openai", "openrouter", "ollama"):
        cfg.provider = provider
        assert cache_model_settings(cfg) == {}
