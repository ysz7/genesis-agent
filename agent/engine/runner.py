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
class Done:
    """Terminal event: the run's ``AgentRunResult`` (``.output`` / ``.usage``)."""
    result: Any


async def iter_events(
    agent: Agent, task: str, deps: AgentDeps, *, message_history=None
) -> AsyncIterator[Any]:
    """Run *agent* on *task*, yielding :class:`Reason` / :class:`ToolCall` /
    :class:`ToolResult` events as they happen, then a final :class:`Done`.

    Does NOT enter ``async with agent`` — the caller manages that lifecycle.
    """
    pending: dict[str, tuple[str, Any]] = {}
    async with agent.iter(
        task,
        deps=deps,
        usage_limits=deps.config.usage_limits,
        message_history=message_history,
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
