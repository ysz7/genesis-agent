"""Phase 35: todo-driven autonomous continuation (autoloop).

Gating is unit-tested directly; the loop behaviour is driven through a real
`build_agent` + `iter_events` with a FunctionModel that completes one plan step
per pass, so the pass count and stop conditions are observable end-to-end.
"""

import asyncio
import json
from types import SimpleNamespace

from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from agent.engine import factory
from agent.engine.factory import build_agent
from agent.engine.runner import Continue, Done, iter_events
from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps


# ── gating helpers ────────────────────────────────────────────────────────────

def test_autoloop_gating():
    from agent.engine.autoloop import autoloop_enabled, max_iterations, plan_has_open_steps

    assert autoloop_enabled({}) is False
    assert autoloop_enabled({"planning": {"enabled": True}}) is False          # autoloop off
    assert autoloop_enabled({"planning": {"enabled": True, "autoloop": True}}) is True
    assert autoloop_enabled({"planning": {"enabled": False, "autoloop": True}}) is False
    # a cap of 1 pass means no continuation, so autoloop is effectively disabled
    assert autoloop_enabled(
        {"planning": {"enabled": True, "autoloop": True, "max_iterations": 1}}
    ) is False

    assert max_iterations({}) == 8
    assert max_iterations({"planning": {"max_iterations": 3}}) == 3
    assert max_iterations({"planning": {"max_iterations": 0}}) == 1             # min 1
    assert max_iterations({"planning": {"max_iterations": "x"}}) == 8           # bad → default

    assert plan_has_open_steps(SimpleNamespace(extra={})) is False
    assert plan_has_open_steps(SimpleNamespace(extra={"plan": [{"status": "done"}]})) is False
    assert plan_has_open_steps(
        SimpleNamespace(extra={"plan": [{"status": "done"}, {"status": "pending"}]})
    ) is True


# ── end-to-end loop behaviour ────────────────────────────────────────────────

def _just_updated_plan(messages) -> bool:
    """True when the last message carries an update_plan tool result (this pass)."""
    return any(
        isinstance(p, ToolReturnPart) and p.tool_name == "update_plan"
        for p in messages[-1].parts
    )


def _plan_updates_so_far(messages) -> int:
    return sum(
        1 for m in messages for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == "update_plan"
    )


def _steps(total_steps: int, n_done: int) -> list[dict]:
    return [
        {"title": f"step{i}", "status": "done" if i < n_done else "pending"}
        for i in range(total_steps)
    ]


def _model(decide, calls: list):
    """A streamable FunctionModel driven by *decide(messages) -> (kind, payload)*.

    ``iter_events`` streams, so the stream_function carries the logic (and records
    each tool call in *calls*); the plain ``function`` mirrors it for completeness.
    """
    def fn(messages, info: AgentInfo) -> ModelResponse:
        kind, payload = decide(messages)
        if kind == "text":
            return ModelResponse(parts=[TextPart(content=payload)])
        return ModelResponse(parts=[ToolCallPart(tool_name="update_plan", args=payload)])

    async def sfn(messages, info: AgentInfo):
        kind, payload = decide(messages)
        if kind == "text":
            yield payload
        else:
            calls.append(sum(1 for s in payload["steps"] if s["status"] == "done"))
            yield {0: DeltaToolCall(name="update_plan", json_args=json.dumps(payload))}

    return FunctionModel(fn, stream_function=sfn)


def _stepwise_model(total_steps: int, calls: list):
    """Marks exactly ONE more plan step done per pass, then ends the pass."""
    def decide(messages):
        if _just_updated_plan(messages):
            return "text", "pass complete"
        n_done = min(_plan_updates_so_far(messages) + 1, total_steps)
        return "tool", {"steps": _steps(total_steps, n_done)}
    return _model(decide, calls)


def _never_done_model(calls: list):
    """Keeps the plan open forever (marks nothing done)."""
    def decide(messages):
        if _just_updated_plan(messages):
            return "text", "still working"
        return "tool", {"steps": _steps(2, 0)}
    return _model(decide, calls)


def _collect(agent, task, deps):
    async def go():
        out = []
        async with agent:
            async for ev in iter_events(agent, task, deps):
                out.append(ev)
        return out
    return asyncio.run(go())


def _usage(result):
    u = result.usage
    return u if hasattr(u, "requests") else u()


_AUTOLOOP = "planning:\n  enabled: true\n  autoloop: true\n  max_iterations: {cap}\n"


def test_autoloop_drives_plan_to_completion(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(factory, "build_model", lambda config: _stepwise_model(3, calls))
    (tmp_path / "settings.yaml").write_text(_AUTOLOOP.format(cap=8), encoding="utf-8")
    config = load_config(tmp_path)
    agent = build_agent(config)
    deps = build_deps(config)
    try:
        events = _collect(agent, "work the plan", deps)
        continues = [e for e in events if isinstance(e, Continue)]
        assert calls == [1, 2, 3]                 # one more step done each pass
        assert len(continues) == 2                # 3 passes → 2 continuation markers
        assert isinstance(events[-1], Done)
        assert events[-1].result.output == "pass complete"
        # Shared usage spans the passes (not reset per pass): more than one pass' worth.
        assert _usage(events[-1].result).requests >= 4
    finally:
        close_deps(deps)


def test_autoloop_capped_by_max_iterations(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(factory, "build_model", lambda config: _never_done_model(calls))
    (tmp_path / "settings.yaml").write_text(_AUTOLOOP.format(cap=3), encoding="utf-8")
    config = load_config(tmp_path)
    agent = build_agent(config)
    deps = build_deps(config)
    try:
        events = _collect(agent, "never finishes", deps)
        assert len(calls) == 3                    # exactly max_iterations passes, then stop
        assert len([e for e in events if isinstance(e, Continue)]) == 2
        assert isinstance(events[-1], Done)
    finally:
        close_deps(deps)


def test_autoloop_off_by_default_single_pass(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(factory, "build_model", lambda config: _stepwise_model(3, calls))
    # planning on, autoloop absent (default off) → today's single-pass behaviour
    (tmp_path / "settings.yaml").write_text("planning:\n  enabled: true\n", encoding="utf-8")
    config = load_config(tmp_path)
    agent = build_agent(config)
    deps = build_deps(config)
    try:
        events = _collect(agent, "one pass only", deps)
        assert calls == [1]                       # a single pass, even though steps remain
        assert not any(isinstance(e, Continue) for e in events)
        assert isinstance(events[-1], Done)
    finally:
        close_deps(deps)
