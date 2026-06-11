"""Phase 3 run controls: UsageLimits from settings + model_settings + enforcement."""

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from agent.runtime.config import load_config


def test_limits_built_from_settings(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "limits:\n  request_limit: 7\n  total_tokens_limit: 1000\n", encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert isinstance(cfg.usage_limits, UsageLimits)
    assert cfg.usage_limits.request_limit == 7
    assert cfg.usage_limits.total_tokens_limit == 1000


def test_no_limits_when_absent(tmp_path):
    (tmp_path / "settings.yaml").write_text("name: x\n", encoding="utf-8")
    assert load_config(tmp_path).usage_limits is None


def test_model_settings_parsed(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "model_settings:\n  temperature: 0\n  max_tokens: 16\n", encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.model_settings == {"temperature": 0, "max_tokens": 16}


def test_request_limit_halts_runaway_loop():
    """A model that always calls the same tool would loop forever — the
    request_limit must stop it with UsageLimitExceeded rather than running away."""

    def always_call_tool(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("ping", {})])

    def ping() -> str:
        """Always returns pong."""
        return "pong"

    agent = Agent(FunctionModel(always_call_tool), tools=[ping])
    with pytest.raises(UsageLimitExceeded):
        agent.run_sync("go", usage_limits=UsageLimits(request_limit=3))
