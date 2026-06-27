"""Phase 21: input/output guardrails (opt-in regex content layer)."""

import socket

import httpx
import pytest
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart

from agent.engine import factory, guardrails
from agent.engine.factory import build_agent
from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.server import start_background, stop_background


def _text_model(text: str) -> FunctionModel:
    def fn(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])
    return FunctionModel(fn)


# ── Input (unit) ─────────────────────────────────────────────────────────────

def test_input_block_and_redact_and_disabled():
    s = {"guardrails": {"input": {"block": ["(?i)forbidden"], "redact": [r"\d{16}"]}}}
    ok, msg = guardrails.check_input(s, "this is FORBIDDEN")
    assert ok is False and "blocked" in msg.lower()
    ok, text = guardrails.check_input(s, "card 1234567812345678 here")
    assert ok is True and "[redacted]" in text and "1234567812345678" not in text
    # no guardrails configured → passes through unchanged
    ok, text = guardrails.check_input({}, "anything goes")
    assert ok is True and text == "anything goes"


# ── Output (through a real agent) ────────────────────────────────────────────

def test_output_redact(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "build_model", lambda config: _text_model("card 1234567812345678 ok"))
    (tmp_path / "settings.yaml").write_text(
        "guardrails:\n  output:\n    redact: ['\\d{16}']\n", encoding="utf-8"
    )
    config = load_config(tmp_path)
    deps = build_deps(config)
    try:
        result = build_agent(config).run_sync("hi", deps=deps)
        assert "[redacted]" in result.output and "1234567812345678" not in result.output
    finally:
        close_deps(deps)


def test_output_block_retries_then_fails(tmp_path, monkeypatch):
    # The model always returns disallowed content → validator retries → run fails.
    monkeypatch.setattr(factory, "build_model", lambda config: _text_model("the secret_token is X"))
    (tmp_path / "settings.yaml").write_text(
        "retries: 1\nguardrails:\n  output:\n    block: ['secret_token']\n", encoding="utf-8"
    )
    config = load_config(tmp_path)
    deps = build_deps(config)
    try:
        with pytest.raises(Exception):
            build_agent(config).run_sync("hi", deps=deps)
    finally:
        close_deps(deps)


def test_disabled_output_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "build_model", lambda config: _text_model("card 1234567812345678"))
    (tmp_path / "settings.yaml").write_text("name: x\n", encoding="utf-8")
    config = load_config(tmp_path)
    deps = build_deps(config)
    try:
        result = build_agent(config).run_sync("hi", deps=deps)
        assert result.output == "card 1234567812345678"   # no redaction, untouched
    finally:
        close_deps(deps)


# ── Input refused at a run site (server) ─────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_server_refuses_blocked_input(tmp_path, monkeypatch):
    called = {"n": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        called["n"] += 1
        return ModelResponse(parts=[TextPart(content="ran")])

    (tmp_path / "settings.yaml").write_text(
        "guardrails:\n  input:\n    block: ['(?i)forbidden']\n", encoding="utf-8"
    )
    monkeypatch.setattr(factory, "build_model", lambda config: FunctionModel(fn))
    config = load_config(tmp_path)
    port = _free_port()
    httpd, deps = start_background(config, port=port)
    base = f"http://127.0.0.1:{port}"
    try:
        r = httpx.post(f"{base}/task", json={"task": "do something FORBIDDEN"}, timeout=10)
        assert r.status_code == 400
        assert "blocked" in r.json()["error"].lower()
        assert called["n"] == 0          # the model was never invoked
    finally:
        stop_background(httpd, deps)
