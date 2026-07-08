"""Config loader: reads settings.yaml + persona.md, applies provider defaults."""

import logging
import re
from pathlib import Path

from agent.runtime.config import KNOWN_SETTINGS_KEYS, load_config


def test_defaults_when_empty(tmp_path, monkeypatch):
    for var in ("PROVIDER", "MODEL", "API_KEY", "BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config(tmp_path)
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.persona  # falls back to a built-in general persona
    assert (tmp_path / "workspace").is_dir()  # created on load


def test_reads_files(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER", "ollama")
    monkeypatch.setenv("MODEL", "llama3.1:8b")
    (tmp_path / "persona.md").write_text("You are a test bot.", encoding="utf-8")
    (tmp_path / "settings.yaml").write_text(
        "name: testbot\nfeeds: [a, b]\n", encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.provider == "ollama"
    assert cfg.persona == "You are a test bot."
    assert cfg.settings["feeds"] == ["a", "b"]
    assert cfg.agent_name == "testbot"


def test_ollama_base_url_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER", "ollama")
    monkeypatch.delenv("BASE_URL", raising=False)
    cfg = load_config(tmp_path)
    assert cfg.base_url == "http://localhost:11434/v1"


def test_typo_key_warns_with_suggestion(tmp_path, caplog):
    # `schedular:` silently disables the scheduler — the guard should catch it.
    (tmp_path / "settings.yaml").write_text(
        "schedular:\n  enabled: true\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="agent.config"):
        load_config(tmp_path)
    assert "schedular" in caplog.text and "scheduler" in caplog.text


def test_custom_vertical_keys_stay_silent(tmp_path, caplog):
    # A vertical's own keys resemble nothing known — no false warning.
    # `thresholds` is the closest collision (vs `threads`) and must stay silent.
    (tmp_path / "settings.yaml").write_text(
        "symbols: [AAPL, MSFT]\nfeeds: [https://example.com/rss]\nthresholds:\n"
        "  max_position: 1000\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="agent.config"):
        load_config(tmp_path)
    assert caplog.records == []


def test_known_keys_cover_documented_template():
    # Drift guard: every top-level key documented in the shipped settings.yaml
    # (active or commented) must be recognized, so the template and the guard
    # can't drift apart. Vertical-example keys (symbols/feeds/…) are excluded.
    template = Path(__file__).resolve().parents[1] / "settings.yaml"
    text = template.read_text(encoding="utf-8").split("Your vertical's settings")[0]
    documented = set()
    for ln in text.splitlines():
        if ln[:1].isspace():
            continue                       # nested key, not top-level
        s = ln[1:] if ln.startswith("#") else ln
        s = s[1:] if s.startswith(" ") else s
        if s[:1].isspace():
            continue                       # was "#   nested"
        m = re.match(r"^([a-z][a-z0-9_]*):[ \t]*(.*)$", s)
        if not m:
            continue
        val = m.group(2).strip()
        # a real YAML declaration has an empty or scalar/list/map value — skip
        # prose comments like "confirm: tools that need approval ..."
        if val and not re.match(r"^(\[|\{|-?\d|true|false|null|\S+$)", val):
            continue
        documented.add(m.group(1))
    missing = documented - KNOWN_SETTINGS_KEYS
    assert not missing, f"settings.yaml documents unknown-to-guard keys: {missing}"
