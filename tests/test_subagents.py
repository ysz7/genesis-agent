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
from agent.tools.subagents import delegate, delegate_to

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


def _write_agent_file(root, name, body):
    agents = root / "workspace" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(body, encoding="utf-8")


_RESEARCHER = """---
description: Researches a topic and returns a brief.
tools:
  allow: [read_file, fetch_url]
---
You are a research sub-agent. Return a tight brief.
"""


def test_load_specs_discovers_and_skips_malformed(tmp_path):
    from agent.tools.subagents import load_specs

    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    _write_agent_file(tmp_path, "researcher", _RESEARCHER)
    _write_agent_file(tmp_path, "broken", "---\n: not: valid: yaml:\n---\nbody")
    _write_agent_file(tmp_path, "_hidden", _RESEARCHER)  # underscore = skipped

    specs = load_specs(tmp_path / "workspace")
    names = [s.name for s in specs]
    assert names == ["researcher"]  # broken + _hidden excluded
    assert specs[0].description.startswith("Researches")
    assert specs[0].allow == ["read_file", "fetch_url"]
    assert "research sub-agent" in specs[0].persona


def test_agents_overview_lists_roster(tmp_path):
    from agent.tools.subagents import agents_overview

    assert agents_overview(tmp_path / "workspace") == ""  # empty → nothing
    _write_agent_file(tmp_path, "researcher", _RESEARCHER)
    overview = agents_overview(tmp_path / "workspace")
    assert "researcher" in overview and "delegate_to" in overview


def test_delegate_to_uses_persona_and_restricted_tools(tmp_path, monkeypatch):
    seen = {}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        seen["tools"] = {t.name for t in info.function_tools}
        seen["system"] = " ".join(
            p.content for m in messages for p in m.parts
            if type(p).__name__ == "SystemPromptPart"
        )
        return ModelResponse(parts=[TextPart(content="brief")])

    monkeypatch.setattr(factory, "build_model", lambda config: FunctionModel(fn))
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    _write_agent_file(tmp_path, "researcher", _RESEARCHER)
    deps = build_deps(load_config(tmp_path))
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        out = delegate_to(ctx, "researcher", "look into X")
        assert out == "brief"
        # persona override reached the model
        assert "research sub-agent" in seen["system"]
        # allow-list honored: only those tools (+ no write_tool/write_agent/delegate*)
        assert seen["tools"] == {"read_file", "fetch_url"}
    finally:
        close_deps(deps)


def test_delegate_to_model_override(tmp_path, monkeypatch):
    """A spec's `model:` swaps the model id (same provider) for that sub-agent."""
    seen = {}

    def build(config):
        seen["model"] = config.model
        return _echo_model()

    monkeypatch.setattr(factory, "build_model", build)
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    _write_agent_file(
        tmp_path, "fast",
        "---\ndescription: cheap specialist\nmodel: gpt-4o-mini\n---\nYou are fast.\n",
    )
    deps = build_deps(load_config(tmp_path))
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        assert delegate_to(ctx, "fast", "do it") == "SUB-ANSWER"
        assert seen["model"] == "gpt-4o-mini"  # override reached build_model
    finally:
        close_deps(deps)


def test_delegate_to_unknown_name_lists_known(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "build_model", lambda config: _echo_model())
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    _write_agent_file(tmp_path, "researcher", _RESEARCHER)
    deps = build_deps(load_config(tmp_path))
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        out = delegate_to(ctx, "missing", "task")
        assert "no subagent named" in out and "researcher" in out
    finally:
        close_deps(deps)


def test_write_agent_creates_usable_definition(tmp_path):
    from agent.tools.subagents import write_agent, load_spec

    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    deps = build_deps(load_config(tmp_path))
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        msg = write_agent(ctx, "reviewer", "Reviews code.", "You review code.", ["read_file"])
        assert "reviewer" in msg
        spec = load_spec(tmp_path / "workspace", "reviewer")
        assert spec is not None
        assert spec.description == "Reviews code."
        assert spec.allow == ["read_file"]
        assert spec.persona == "You review code."
        # bad name rejected
        assert "invalid" in write_agent(ctx, "bad name!", "d", "p")
    finally:
        close_deps(deps)


def test_write_agent_edit_requires_approval(tmp_path):
    """Creating a subagent is free; updating an existing one is gated."""
    from agent.tools.subagents import write_agent, load_spec

    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    deps = build_deps(load_config(tmp_path))
    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    try:
        # new = free, no approval hook needed
        assert "Saved" in write_agent(ctx, "rev", "Reviews.", "v1 persona")
        # edit with no approval channel (headless) = declined, file unchanged
        msg = write_agent(ctx, "rev", "Reviews.", "v2 persona")
        assert "declined" in msg
        assert load_spec(tmp_path / "workspace", "rev").persona == "v1 persona"
        # edit with an approving hook = applied
        deps.approval_hook = lambda subject, detail: "once"
        assert "Updated" in write_agent(ctx, "rev", "Reviews.", "v2 persona")
        assert load_spec(tmp_path / "workspace", "rev").persona == "v2 persona"
    finally:
        close_deps(deps)


def test_authoring_tools_gated_by_allow_authoring(tmp_path):
    on = tmp_path / "on"
    on.mkdir()
    (on / "settings.yaml").write_text(_ON, encoding="utf-8")  # allow_authoring defaults true
    names = tool_names(discover_tools(load_config(on)))
    assert "write_agent" in names and "delegate_to" in names

    off = tmp_path / "off"
    off.mkdir()
    (off / "settings.yaml").write_text(
        "subagents:\n  enabled: true\n  allow_authoring: false\n", encoding="utf-8"
    )
    names = tool_names(discover_tools(load_config(off)))
    assert "delegate_to" in names and "write_agent" not in names


def test_end_to_end_delegate_to(tmp_path, monkeypatch):
    """A real parent agent reads the roster, calls delegate_to, the named
    sub-agent (no delegate_to at max_depth) answers, result folds back."""

    def role_aware(messages, info: AgentInfo) -> ModelResponse:
        names = {t.name for t in info.function_tools}
        delegated = any(
            isinstance(p, ToolReturnPart) and p.tool_name == "delegate_to"
            for m in messages for p in m.parts
        )
        if "delegate_to" in names and not delegated:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="delegate_to", args={"name": "researcher", "task": "look into X"}
            )])
        return ModelResponse(parts=[TextPart(
            content="final:delegated" if delegated else "final:direct"
        )])

    monkeypatch.setattr(factory, "build_model", lambda config: FunctionModel(role_aware))
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    _write_agent_file(tmp_path, "researcher", _RESEARCHER)
    config = load_config(tmp_path)
    agent = build_agent(config)
    deps = build_deps(config)
    try:
        result = agent.run_sync("do it via the researcher", deps=deps)
        assert result.output == "final:delegated"
    finally:
        close_deps(deps)


def test_menu_subagents_lists_and_empty_state(tmp_path, monkeypatch):
    """The menu action reads the live roster and shows the empty state."""
    from agent.console import menu

    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    captured = {}

    def fake_select(title, options, subtitle="", index=0):
        captured["options"] = options
        captured["subtitle"] = subtitle
        return None  # Esc → return without opening a detail view

    monkeypatch.setattr(menu, "select", fake_select)

    menu._subagents(tmp_path)  # empty
    assert captured["options"] == ["Back"]
    assert "no subagents yet" in captured["subtitle"]

    _write_agent_file(tmp_path, "researcher", _RESEARCHER)
    menu._subagents(tmp_path)  # populated
    assert any("researcher" in o for o in captured["options"])


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
