"""Phase 14: subagents / delegation — isolation, depth guard, restricted tools."""

from types import SimpleNamespace

from pydantic_ai import Agent
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import RunUsage

from agent.engine import factory
from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.engine.factory import build_agent
from agent.engine.registry import discover_tools, tool_names
from agent.tools.subagents import delegate

_ON = "subagents:\n  enabled: true\n  max_depth: 1\n"


def _echo_model(text="SUB-ANSWER"):
    def fn(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])
    return FunctionModel(fn)


def test_registered_only_when_enabled(tmp_path):
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    assert "delegate" in tool_names(discover_tools(load_config(tmp_path)))

    off = tmp_path / "off"
    off.mkdir()
    (off / "settings.yaml").write_text("name: off\n", encoding="utf-8")
    assert "delegate" not in tool_names(discover_tools(load_config(off)))


def test_exclude_tools_builds_restricted_toolset(tmp_path):
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    config = load_config(tmp_path)
    names = tool_names(discover_tools(config, exclude={"delegate", "run_shell"}))
    assert "delegate" not in names and "run_shell" not in names
    assert "read_file" in names  # others still present


def test_delegate_runs_subagent_and_folds_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "build_model", lambda config: _echo_model())
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    deps = build_deps(load_config(tmp_path))
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        out = delegate(ctx, "do a subtask")
        assert out == "SUB-ANSWER"
        assert ctx.usage.requests >= 1  # sub-agent cost folded into the parent
    finally:
        close_deps(deps)


def test_delegate_depth_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "build_model", lambda config: _echo_model())
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    deps = build_deps(load_config(tmp_path))
    deps.extra["delegation_depth"] = 1  # already at max_depth
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        out = delegate(ctx, "go deeper")
        assert "depth limit" in out
    finally:
        close_deps(deps)


def test_delegate_isolates_context_no_history():
    """The sub-agent must run with a clean history — assert run_sync gets no
    message_history (isolation is the whole point)."""
    seen = {}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        seen["n_messages"] = len(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    from agent.engine import factory as f
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "settings.yaml").write_text(_ON, encoding="utf-8")
    orig = f.build_model
    f.build_model = lambda config: FunctionModel(fn)
    try:
        deps = build_deps(load_config(tmp))
        ctx = SimpleNamespace(deps=deps, usage=RunUsage())
        delegate(ctx, "isolated task")
        # Only the system prompt(s) + the single user task — no prior turns.
        assert seen["n_messages"] == 1
        close_deps(deps)
    finally:
        f.build_model = orig


def test_end_to_end_parent_delegates(tmp_path, monkeypatch):
    """Through real agents: the parent calls delegate, the sub-agent (which has no
    delegate tool at max_depth) answers, and the result is folded back."""

    def role_aware(messages, info: AgentInfo) -> ModelResponse:
        names = {t.name for t in info.function_tools}
        delegated = any(
            isinstance(p, ToolReturnPart) and p.tool_name == "delegate"
            for m in messages for p in m.parts
        )
        if "delegate" in names and not delegated:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="delegate", args={"task": "the subtask"}
            )])
        return ModelResponse(parts=[TextPart(content="final:delegated" if delegated else "final:direct")])

    monkeypatch.setattr(factory, "build_model", lambda config: FunctionModel(role_aware))
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    config = load_config(tmp_path)
    agent = build_agent(config)
    deps = build_deps(config)
    try:
        result = agent.run_sync("do it via a subagent", deps=deps)
        # Parent saw the delegate result → "final:delegated"; the sub-agent had
        # no delegate tool (max_depth=1) so it answered directly.
        assert result.output == "final:delegated"
    finally:
        close_deps(deps)
