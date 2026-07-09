"""Phase 31: verify / self-critique loop (evaluator–optimizer).

A single FunctionModel plays both roles — it returns a verdict when handed the
strict-evaluator prompt and an answer otherwise — so the actor/critic split is
exercised without a network. Usage is real (FunctionModel reports requests).
"""

import asyncio
from types import SimpleNamespace

from pydantic_ai import Agent
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart

from agent.engine.verify import (
    _is_trivial,
    _parse_verdict,
    verification_enabled,
    verify_and_revise,
)


def _last_user(messages) -> str:
    for m in reversed(messages):
        for p in m.parts:
            if isinstance(p, UserPromptPart):
                return str(p.content)
    return ""


def _deps(**verify):
    settings = {"verify": verify} if verify else {}
    return SimpleNamespace(settings=settings, config=SimpleNamespace(usage_limits=None))


LONG = "x" * 500  # over the 400-char trivial threshold


def _run(coro):
    return asyncio.run(coro)


# ── unit: helpers ────────────────────────────────────────────────────────────

def test_verification_enabled():
    assert verification_enabled({"verify": {"enabled": True}}) is True
    assert verification_enabled({"verify": {"enabled": True, "max_rounds": 0}}) is False
    assert verification_enabled({"verify": {"enabled": False}}) is False
    assert verification_enabled({}) is False


def test_parse_verdict_lenient():
    v = _parse_verdict('noise {"ok": false, "weakest": "depth", "gaps": "add X"} tail')
    assert v.ok is False and v.weakest == "depth" and v.gaps == "add X"
    # Unparseable → fail-open (ok), never loop blind.
    assert _parse_verdict("not json at all").ok is True


def test_is_trivial():
    short = SimpleNamespace(output="hi", all_messages=lambda: [])
    assert _is_trivial(short, 400) is True
    long = SimpleNamespace(output=LONG, all_messages=lambda: [])
    assert _is_trivial(long, 400) is False
    structured = SimpleNamespace(output={"k": 1}, all_messages=lambda: [])
    assert _is_trivial(structured, 400) is True  # non-str output isn't graded


# ── behaviour ────────────────────────────────────────────────────────────────

def _agent(responder):
    return Agent(FunctionModel(responder))


def test_disabled_is_noop():
    agent = _agent(lambda m, i: ModelResponse(parts=[TextPart(content=LONG)]))
    r = _run(agent.run("task"))
    r2 = _run(verify_and_revise(agent, "task", _deps(), r))
    assert r2 is r  # same object, no evaluator call


def test_max_rounds_zero_disables():
    agent = _agent(lambda m, i: ModelResponse(parts=[TextPart(content=LONG)]))
    r = _run(agent.run("task"))
    r2 = _run(verify_and_revise(agent, "task", _deps(enabled=True, max_rounds=0), r))
    assert r2 is r


def test_trivial_answer_skipped():
    # Short answer, no tools → skipped even with verify on.
    agent = _agent(lambda m, i: ModelResponse(parts=[TextPart(content="ok")]))
    r = _run(agent.run("task"))
    r2 = _run(verify_and_revise(agent, "task", _deps(enabled=True), r))
    assert r2 is r


def test_incomplete_answer_triggers_one_revision():
    reruns = []

    def responder(messages, info: AgentInfo) -> ModelResponse:
        text = _last_user(messages)
        if "strict evaluator" in text:
            return ModelResponse(parts=[TextPart(content='{"ok": false, "weakest": "w", "gaps": "g"}')])
        if "Fix the single weakest" in text:
            reruns.append(text)
            return ModelResponse(parts=[TextPart(content="REVISED " + LONG)])
        return ModelResponse(parts=[TextPart(content=LONG)])

    agent = _agent(responder)
    r = _run(agent.run("do it"))
    r2 = _run(verify_and_revise(agent, "do it", _deps(enabled=True, max_rounds=1), r))
    assert r2.output.startswith("REVISED")
    assert len(reruns) == 1  # exactly one revision despite a still-failing verdict


def test_ok_verdict_no_revision():
    def responder(messages, info: AgentInfo) -> ModelResponse:
        text = _last_user(messages)
        if "strict evaluator" in text:
            return ModelResponse(parts=[TextPart(content='{"ok": true}')])
        return ModelResponse(parts=[TextPart(content=LONG)])

    agent = _agent(responder)
    r = _run(agent.run("do it"))
    r2 = _run(verify_and_revise(agent, "do it", _deps(enabled=True), r))
    assert r2 is r  # passed on first try → original returned


def test_notify_fires_on_revision():
    seen = []

    def responder(messages, info: AgentInfo) -> ModelResponse:
        text = _last_user(messages)
        if "strict evaluator" in text:
            return ModelResponse(parts=[TextPart(content='{"ok": false, "weakest": "clarity", "gaps": "g"}')])
        if "Fix the single weakest" in text:
            return ModelResponse(parts=[TextPart(content="REVISED " + LONG)])
        return ModelResponse(parts=[TextPart(content=LONG)])

    agent = _agent(responder)
    r = _run(agent.run("do it"))
    _run(verify_and_revise(agent, "do it", _deps(enabled=True), r, notify=seen.append))
    assert seen == ["clarity"]


def test_unparseable_verdict_fails_open():
    def responder(messages, info: AgentInfo) -> ModelResponse:
        text = _last_user(messages)
        if "strict evaluator" in text:
            return ModelResponse(parts=[TextPart(content="the answer looks fine to me")])
        return ModelResponse(parts=[TextPart(content=LONG)])

    agent = _agent(responder)
    r = _run(agent.run("do it"))
    r2 = _run(verify_and_revise(agent, "do it", _deps(enabled=True), r))
    assert r2 is r  # no JSON verdict → treated as ok, no revision


def test_usage_folds_first_eval_and_rerun():
    def responder(messages, info: AgentInfo) -> ModelResponse:
        text = _last_user(messages)
        if "strict evaluator" in text:
            return ModelResponse(parts=[TextPart(content='{"ok": false, "weakest": "w", "gaps": "g"}')])
        if "Fix the single weakest" in text:
            return ModelResponse(parts=[TextPart(content="REVISED " + LONG)])
        return ModelResponse(parts=[TextPart(content=LONG)])

    agent = _agent(responder)
    r = _run(agent.run("do it"))
    r2 = _run(verify_and_revise(agent, "do it", _deps(enabled=True, max_rounds=1), r))
    # The re-run bumps the request counter (the evaluator is a bare model_request,
    # which adds tokens but not a "request"); the evaluator's sizable input prompt
    # plus the re-run push input/output tokens above the first run's alone.
    assert r2.usage.requests == r.usage.requests + 1          # re-run folded
    assert r2.usage.input_tokens > r.usage.input_tokens       # evaluator + re-run folded
    assert r2.usage.output_tokens > r.usage.output_tokens     # re-run output folded
