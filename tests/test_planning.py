"""Phase 13: explicit planning / todo scratchpad."""

from types import SimpleNamespace

from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.engine.factory import build_agent
from agent.engine.registry import discover_tools, tool_names
from agent.tools.planning import (
    MAX_STEPS, MAX_TITLE, PlanStep, plan_overview, render_plan, update_plan,
)

_ON = "planning:\n  enabled: true\n"


def _ctx(tmp_path):
    deps = build_deps(load_config(tmp_path))
    return SimpleNamespace(deps=deps), deps


def test_update_plan_stores_and_renders(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        out = update_plan(ctx, [
            PlanStep(title="gather facts", status="done"),
            PlanStep(title="draft answer", status="in_progress"),
            PlanStep(title="review"),  # defaults to pending
        ])
        assert deps.extra["plan"][0] == {"title": "gather facts", "status": "done"}
        assert deps.extra["plan"][2]["status"] == "pending"
        assert "✓ gather facts" in out and "▸ draft answer" in out and "○ review" in out
    finally:
        close_deps(deps)


def test_update_plan_caps_steps_and_titles(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        update_plan(ctx, [PlanStep(title=f"s{i}") for i in range(MAX_STEPS + 10)])
        assert len(deps.extra["plan"]) == MAX_STEPS
        update_plan(ctx, [PlanStep(title="x" * (MAX_TITLE + 50))])
        assert len(deps.extra["plan"][0]["title"]) == MAX_TITLE
    finally:
        close_deps(deps)


def test_plan_overview_empty_then_populated(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        assert plan_overview(deps) == ""        # nothing yet
        update_plan(ctx, [PlanStep(title="only step", status="in_progress")])
        ov = plan_overview(deps)
        assert "Current plan" in ov and "only step" in ov
    finally:
        close_deps(deps)


def test_registered_only_when_enabled(tmp_path):
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")
    assert "update_plan" in tool_names(discover_tools(load_config(tmp_path)))

    off = tmp_path / "off"
    off.mkdir()
    (off / "settings.yaml").write_text("name: off\n", encoding="utf-8")
    assert "update_plan" not in tool_names(discover_tools(load_config(off)))


def test_end_to_end_model_drives_the_plan(tmp_path):
    """A model that calls update_plan populates the per-run plan in deps."""
    (tmp_path / "settings.yaml").write_text(_ON, encoding="utf-8")

    def planner():
        n = {"i": 0}

        def fn(messages, info: AgentInfo) -> ModelResponse:
            n["i"] += 1
            if n["i"] == 1:
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="update_plan",
                    args={"steps": [
                        {"title": "step one", "status": "in_progress"},
                        {"title": "step two", "status": "pending"},
                    ]},
                )])
            return ModelResponse(parts=[TextPart(content="done")])

        return fn

    config = load_config(tmp_path)
    agent = build_agent(config)
    deps = build_deps(config)
    try:
        with agent.override(model=FunctionModel(planner())):
            result = agent.run_sync("do a two-step task", deps=deps)
        assert result.output == "done"
        assert deps.extra["plan"][0] == {"title": "step one", "status": "in_progress"}
        assert deps.extra["plan"][1]["title"] == "step two"
    finally:
        close_deps(deps)


def test_render_plan_markers():
    plan = [{"title": "a", "status": "done"}, {"title": "b", "status": "pending"}]
    assert render_plan(plan) == "✓ a\n○ b"
