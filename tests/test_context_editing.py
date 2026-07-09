"""Phase 30: context editing — stale tool-result pruning.

The pruning function is pure; the processor is a plain async callable gated on
the context budget. Exercised directly (no network), plus one order-stable pass
composed with compaction.
"""

import asyncio
from types import SimpleNamespace

from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from agent.engine.compaction import (
    CLEARED_MARK,
    build_context_editor,
    prune_tool_outputs,
)


def _cfg(**settings):
    return SimpleNamespace(settings=settings)


def _ctx():
    return SimpleNamespace(usage=RunUsage())


def _tool_round(tool: str, out: str, cid: str) -> list:
    """A call/result pair: response carries the call, request the result."""
    return [
        ModelRequest(parts=[UserPromptPart(content=f"run {tool}")]),
        ModelResponse(parts=[ToolCallPart(tool_name=tool, args={}, tool_call_id=cid)]),
        ModelRequest(parts=[ToolReturnPart(tool_name=tool, content=out, tool_call_id=cid)]),
        ModelResponse(parts=[TextPart(content="done")]),
    ]


# ── The pure pruning function ────────────────────────────────────────────────

def test_large_old_output_is_stubbed():
    big = "x" * 5000
    messages = _tool_round("run_shell", big, "c1") + _tool_round("fetch_url", "y" * 8, "c2")
    out = prune_tool_outputs(messages, keep_last=2, min_chars=2000)

    assert len(out) == len(messages)  # message count unchanged
    ret = next(
        p for m in out for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_call_id == "c1"
    )
    assert ret.content == CLEARED_MARK.format(tool="run_shell", n=5000)


def test_pairing_is_preserved():
    big = "z" * 4000
    messages = _tool_round("run_shell", big, "c1") + [
        ModelRequest(parts=[UserPromptPart(content="p" * 50)]),
        ModelResponse(parts=[TextPart(content="q" * 50)]),
    ]
    out = prune_tool_outputs(messages, keep_last=2, min_chars=2000)

    call_ids = {p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolCallPart)}
    return_ids = {p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolReturnPart)}
    assert return_ids == call_ids == {"c1"}  # the result still has its call


def test_recent_window_untouched():
    big = "x" * 5000
    messages = _tool_round("run_shell", big, "c1")
    # keep_last covers the whole tail → nothing eligible.
    out = prune_tool_outputs(messages, keep_last=len(messages), min_chars=2000)
    assert out is messages  # no change, same object


def test_small_outputs_left_alone():
    messages = _tool_round("run_shell", "small output", "c1") + _tool_round(
        "fetch_url", "also small", "c2"
    )
    out = prune_tool_outputs(messages, keep_last=0, min_chars=2000)
    assert out is messages  # every body under min_chars


def test_copy_on_write_keeps_originals_intact():
    big = "x" * 5000
    messages = _tool_round("run_shell", big, "c1") + _tool_round("fetch_url", "y", "c2")
    original_part = next(
        p for m in messages for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_call_id == "c1"
    )
    prune_tool_outputs(messages, keep_last=2, min_chars=2000)
    assert original_part.content == big  # source untouched (persisted thread safe)


def test_stub_is_idempotent():
    big = "x" * 5000
    messages = _tool_round("run_shell", big, "c1") + _tool_round("fetch_url", "y", "c2")
    once = prune_tool_outputs(messages, keep_last=2, min_chars=2000)
    twice = prune_tool_outputs(once, keep_last=2, min_chars=2000)
    assert twice is once  # stub is < min_chars → never re-pruned


def test_non_tool_parts_untouched():
    messages = [
        ModelRequest(parts=[UserPromptPart(content="hello " + "h" * 5000)]),
        ModelResponse(parts=[TextPart(content="hi " + "t" * 5000)]),
        *_tool_round("run_shell", "small", "c1"),
    ]
    out = prune_tool_outputs(messages, keep_last=0, min_chars=2000)
    assert out is messages  # only ToolReturnPart bodies are candidates


# ── The processor (budget-gated) ─────────────────────────────────────────────

def test_disabled_returns_none():
    assert build_context_editor(_cfg(context_editing={"enabled": False})) is None


def test_on_by_default_without_block():
    assert build_context_editor(_cfg()) is not None


def test_under_budget_is_passthrough():
    proc = build_context_editor(_cfg(context_budget=100000))
    big = "x" * 5000
    messages = _tool_round("run_shell", big, "c1")
    out = asyncio.run(proc(_ctx(), messages))
    assert out is messages  # plenty of room → keep detail verbatim


def test_over_budget_prunes():
    # threshold = 60% of 100 tokens = 240 chars; the 5000-char body blows past it.
    proc = build_context_editor(
        _cfg(context_budget=100, context_editing={"keep_last": 2, "min_chars": 2000})
    )
    big = "x" * 5000
    messages = _tool_round("run_shell", big, "c1") + _tool_round("fetch_url", "y" * 8, "c2")
    out = asyncio.run(proc(_ctx(), messages))
    ret = next(
        p for m in out for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_call_id == "c1"
    )
    assert ret.content.startswith("[tool output cleared")


# ── Order-stable composition with compaction ─────────────────────────────────

def test_prune_then_compact_is_order_stable():
    """Pruning runs first; compaction then summarizes the smaller transcript.

    Mirrors factory's chain so the interaction is covered end-to-end.
    """
    from agent.engine.compaction import build_history_processor

    def _summarizer(messages, info: AgentInfo) -> ModelResponse:
        text = "".join(str(getattr(p, "content", "")) for m in messages for p in m.parts)
        return ModelResponse(parts=[TextPart(content="SUMMARY " + text)])

    cfg = _cfg(
        context_budget=100,
        context_editing={"keep_last": 2, "min_chars": 2000},
        compaction={"keep": 2},
    )
    editor = build_context_editor(cfg)
    compactor = build_history_processor(cfg, FunctionModel(_summarizer))

    big = "x" * 5000
    messages = _tool_round("run_shell", big, "c1") + _tool_round("fetch_url", "z" * 3000, "c2")

    async def chain(ctx, msgs):
        for fn in (editor, compactor):
            msgs = await fn(ctx, msgs)
        return msgs

    out = asyncio.run(chain(_ctx(), messages))
    # The summary was built from the pruned transcript, so the giant body never
    # reached it — the stub marker did instead of 5000 x's.
    summary = next(
        (p.content for m in out for p in m.parts
         if isinstance(p, UserPromptPart) and str(p.content).startswith("[conversation summary]")),
        "",
    )
    assert "tool output cleared" in summary
    assert "x" * 5000 not in summary
