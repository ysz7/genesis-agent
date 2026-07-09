"""The single agent event-walk, shared by the CLI tree and the server's SSE.

``iter_events`` drives ``agent.iter`` once and yields neutral, render-free
events (reasoning text, tool calls, tool results, the final result). The rich
console (`console/display.py`) and the headless SSE endpoint (`server/server.py`)
both consume these, so the two renderers can never drift apart — and this module
imports no rich, keeping the server dependency-clean.

The caller owns the agent's async context. The CLI wraps consumption in
``async with agent:`` per run; the server enters it once for the whole serve
lifetime (so MCP servers start once, not per request).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from pydantic_ai import Agent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)

from ..runtime.context import AgentDeps


@dataclass
class Think:
    """A chunk of model *thinking* (extended reasoning) — Phase 29.

    Emitted before :class:`Reason` when the model produces a thinking block. Kept
    distinct so renderers can dim it (CLI) or tag it (SSE) apart from the answer,
    and so it never gets mistaken for final output.
    """
    text: str


@dataclass
class Reason:
    """A chunk of model reasoning/answer text emitted by one model request."""
    text: str


@dataclass
class ToolCall:
    """The model decided to call a tool."""
    name: str
    args: Any


@dataclass
class ToolResult:
    """A tool returned; ``args`` are carried from the matching call."""
    name: str
    args: Any
    content: Any


@dataclass
class Continue:
    """Autoloop is re-entering the agent because the plan still has open steps (Phase 35).

    Emitted between passes of the todo-driven continuation so renderers can mark
    the boundary; ``iteration`` is the pass just completed.
    """
    iteration: int


@dataclass
class Done:
    """Terminal event: the run's ``AgentRunResult`` (``.output`` / ``.usage``)."""
    result: Any


async def iter_events(
    agent: Agent, task: str, deps: AgentDeps, *, message_history=None
) -> AsyncIterator[Any]:
    """Run *agent* on *task*, yielding events as they happen, then a final :class:`Done`.

    With ``planning.autoloop`` on (Phase 35), the walk re-enters the agent after a
    pass whose plan still has open steps — feeding a "continue with the plan" nudge
    and the prior history — until the plan is done or ``max_iterations`` is hit,
    emitting a :class:`Continue` between passes. One shared ``RunUsage`` spans the
    passes so ``usage_limits`` bounds the whole loop. With autoloop off this is a
    single pass, byte-identical to before.

    Does NOT enter ``async with agent`` — the caller manages that lifecycle.
    """
    from .autoloop import (
        autoloop_enabled,
        build_nudge,
        max_iterations,
        plan_has_open_steps,
    )

    loop_on = autoloop_enabled(deps.config.settings)
    shared = None
    if loop_on:
        from pydantic_ai.usage import RunUsage

        shared = RunUsage()  # aggregate budget across passes (bounds the whole loop)

    cap = max_iterations(deps.config.settings) if loop_on else 1
    cur_task, cur_history = task, message_history
    result = None
    passes = 0
    while True:
        async for ev in _run_once(
            agent, cur_task, deps, message_history=cur_history, usage=shared
        ):
            if isinstance(ev, Done):
                result = ev.result  # swallow the per-pass terminal; re-emitted once at the end
            else:
                yield ev
        passes += 1
        if not loop_on or passes >= cap or not plan_has_open_steps(deps):
            break
        yield Continue(passes)
        cur_task, cur_history = build_nudge(deps), result.all_messages()
    yield Done(result)


async def _run_once(
    agent: Agent, task: str, deps: AgentDeps, *, message_history=None, usage=None
) -> AsyncIterator[Any]:
    """One agent pass: walk ``agent.iter`` and yield events, ending with :class:`Done`."""
    pending: dict[str, tuple[str, Any]] = {}
    async with agent.iter(
        task,
        deps=deps,
        usage_limits=deps.config.usage_limits,
        message_history=message_history,
        usage=usage,
    ) as run:
        async for node in run:
            if Agent.is_model_request_node(node):
                text = ""
                thinking = ""
                async with node.stream(run.ctx) as stream:
                    async for event in stream:
                        if isinstance(event, PartStartEvent):
                            if isinstance(event.part, TextPart):
                                text += event.part.content or ""
                            elif isinstance(event.part, ThinkingPart):
                                thinking += event.part.content or ""
                        elif isinstance(event, PartDeltaEvent):
                            if isinstance(event.delta, TextPartDelta):
                                text += event.delta.content_delta or ""
                            elif isinstance(event.delta, ThinkingPartDelta):
                                thinking += event.delta.content_delta or ""
                if thinking.strip():
                    yield Think(thinking)
                if text.strip():
                    yield Reason(text)
            elif Agent.is_call_tools_node(node):
                async with node.stream(run.ctx) as stream:
                    async for event in stream:
                        if isinstance(event, FunctionToolCallEvent):
                            name = event.part.tool_name
                            pending[event.part.tool_call_id] = (name, event.part.args)
                            yield ToolCall(name, event.part.args)
                        elif isinstance(event, FunctionToolResultEvent):
                            name, args = pending.pop(
                                event.part.tool_call_id,
                                (event.part.tool_name, {}),
                            )
                            yield ToolResult(name, args, event.part.content)
        yield Done(run.result)
