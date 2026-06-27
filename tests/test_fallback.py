"""Phase 20: model fallback — wraps the primary in a FallbackModel when
``model_fallbacks`` is set, and a transient primary failure retries the next.
"""

from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.exceptions import ModelHTTPError

from agent.engine import model as model_mod
from agent.engine.model import build_model
from agent.engine.factory import build_agent
from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps


def _good(text="OK") -> FunctionModel:
    def fn(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])
    return FunctionModel(fn)


def _erroring() -> FunctionModel:
    def fn(messages, info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="primary", body="down")
    return FunctionModel(fn)


def test_no_fallbacks_returns_plain_model(tmp_path):
    (tmp_path / "settings.yaml").write_text("name: x\n", encoding="utf-8")
    assert not isinstance(build_model(load_config(tmp_path)), FallbackModel)


def test_fallbacks_wraps_in_fallback_model(tmp_path, monkeypatch):
    monkeypatch.setattr(model_mod, "_build_one", lambda config, mid: _good(mid))
    (tmp_path / "settings.yaml").write_text("model_fallbacks: [backup]\n", encoding="utf-8")
    assert isinstance(build_model(load_config(tmp_path)), FallbackModel)


def test_fallback_answers_when_primary_errors(tmp_path, monkeypatch):
    # primary (config.model) errors with an HTTP error; "backup" answers.
    def stub(config, model_id):
        return _good("FALLBACK") if model_id == "backup" else _erroring()

    monkeypatch.setattr(model_mod, "_build_one", stub)
    (tmp_path / "settings.yaml").write_text("model_fallbacks: [backup]\n", encoding="utf-8")
    config = load_config(tmp_path)
    agent = build_agent(config)          # uses the real build_model → FallbackModel
    deps = build_deps(config)
    try:
        result = agent.run_sync("hi", deps=deps)
        assert result.output == "FALLBACK"
    finally:
        close_deps(deps)
