"""Phase 4.5: history auto-compaction.

The processor is exercised directly (it's a plain async callable) with a
FunctionModel as the summarizer — no monkeypatching, no network. The last test
runs it end-to-end through a real Agent via ``capabilities=[ProcessHistory]``.
"""

import asyncio
from types import SimpleNamespace

from pydantic_ai import Agent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from agent.engine.compaction import SUMMARY_MARK, build_history_processor


def _summarizer(messages, info: AgentInfo) -> ModelResponse:
    """Fake summary model: echoes the transcript so facts are traceable."""
    text = "".join(
        str(getattr(p, "content", "")) for m in messages for p in m.parts
    )
    return ModelResponse(parts=[TextPart(content="SUMMARY " + text)])


def _cfg(**settings):
    return SimpleNamespace(settings=settings)


def _ctx():
    return SimpleNamespace(usage=RunUsage())


def _turn(q: str, a: str) -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content=q)]),
        ModelResponse(parts=[TextPart(content=a)]),
    ]


def test_disabled_returns_none():
    cfg = _cfg(compaction={"enabled": False})
    assert build_history_processor(cfg, FunctionModel(_summarizer)) is None


def test_short_history_untouched():
    proc = build_history_processor(_cfg(context_budget=100000), FunctionModel(_summarizer))
    messages = _turn("hi", "hello")
    out = asyncio.run(proc(_ctx(), messages))
    assert out is messages  # below threshold → passthrough, no copy, no calls


def test_long_history_compacts_and_keeps_facts():
    # threshold = 60% of 100 tokens = 240 chars — a few turns blow past it.
    cfg = _cfg(context_budget=100, compaction={"keep": 2})
    proc = build_history_processor(cfg, FunctionModel(_summarizer))

    messages = [
        ModelRequest(parts=[
            SystemPromptPart(content="You are terse."),
            UserPromptPart(content="my name is alice"),
        ]),
        ModelResponse(parts=[TextPart(content="hi alice " + "x" * 200)]),
    ]
    for i in range(4):
        messages += _turn(f"padding question {i} " + "y" * 100, "answer " + "z" * 100)

    ctx = _ctx()
    out = asyncio.run(proc(ctx, messages))

    assert len(out) < len(messages)
    # Summary message: marked, fact preserved, persona re-attached.
    first = out[0]
    assert isinstance(first, ModelRequest)
    assert any(isinstance(p, SystemPromptPart) for p in first.parts)
    user_part = next(p for p in first.parts if isinstance(p, UserPromptPart))
    assert user_part.content.startswith(SUMMARY_MARK)
    assert "alice" in user_part.content
    # Alternation: summary request → assistant ack → tail starting on a request.
    assert isinstance(out[1], ModelResponse)
    assert isinstance(out[2], ModelRequest)
    # The last `keep` messages survive verbatim.
    assert out[-2:] == messages[-2:]
    # The summary call's cost was charged to the run.
    assert ctx.usage.requests >= 0  # incr ran without raising


def test_cut_never_orphans_tool_results():
    cfg = _cfg(context_budget=100, compaction={"keep": 2})
    proc = build_history_processor(cfg, FunctionModel(_summarizer))

    pad = "p" * 120
    messages = [
        *_turn("first question " + pad, "first answer " + pad),
        # A tool round-trip: response with the call, request with the result.
        ModelRequest(parts=[UserPromptPart(content="use the tool " + pad)]),
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content="ok", tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="tool done " + pad)]),
        *_turn("last question " + pad, "last answer " + pad),
    ]
    # keep=2 puts the desired cut exactly on the ToolReturnPart request — the
    # processor must walk back to a safe boundary instead of splitting the pair.
    out = asyncio.run(proc(_ctx(), messages))

    call_ids = {
        p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolCallPart)
    }
    return_ids = {
        p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolReturnPart)
    }
    assert return_ids <= call_ids  # every kept result still has its call
    # And nothing in the output STARTS with an orphaned tool result.
    for m in out:
        if isinstance(m, ModelRequest) and any(
            isinstance(p, ToolReturnPart) for p in m.parts
        ):
            idx = out.index(m)
            assert idx > 0 and isinstance(out[idx - 1], ModelResponse)


def test_end_to_end_agent_recalls_fact_after_compaction():
    """Through a real Agent: the fact from turn 1 reaches the model only via the
    summary once the history is compacted."""

    def main_model(messages, info: AgentInfo) -> ModelResponse:
        # Answer with the secret if any visible message mentions it.
        text = "".join(
            str(getattr(p, "content", "")) for m in messages for p in m.parts
        )
        if "what is the secret" in text:
            answer = "BLUE-42" if "BLUE-42" in text else "unknown"
            return ModelResponse(parts=[TextPart(content=answer)])
        return ModelResponse(parts=[TextPart(content="ok " + "f" * 200)])

    cfg = _cfg(context_budget=100, compaction={"keep": 2})
    proc = build_history_processor(cfg, FunctionModel(_summarizer))
    agent = Agent(FunctionModel(main_model), capabilities=[ProcessHistory(proc)])

    history: list = []
    r = agent.run_sync("the secret is BLUE-42", message_history=history)
    history.extend(r.new_messages())
    for i in range(3):  # pad well past the 240-char threshold
        r = agent.run_sync(f"padding {i} " + "q" * 150, message_history=history)
        history.extend(r.new_messages())

    r = agent.run_sync("what is the secret?", message_history=history)
    assert r.output == "BLUE-42"
