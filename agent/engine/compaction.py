"""Auto-compaction of message history — long sessions keep their *meaning*.

Phase 4's ``history_keep`` is blunt truncation: old messages vanish, facts from
the start of the session with them. This module builds a Pydantic AI history
processor (``Agent(capabilities=[ProcessHistory(...)])``) that, when the
history grows past a token threshold, replaces everything but the most recent
messages with a single model-written summary. It runs before every model
request, so it covers BOTH long REPL sessions *and* a single long run whose own
tool calls bloat its history.

Settings (``settings.yaml``)::

    context_budget: 100000      # model's usable context, tokens (threshold = 60%)
    compaction:
      enabled: true             # false → Phase 4 truncation only
      keep: 12                  # recent messages kept verbatim
      max_tokens: 1024          # cap on the summary itself

Design notes:

- Token counts are estimated as ``chars / 4`` — close enough for a trigger;
  an exact tokenizer isn't worth the dependency.
- The cut never splits a tool-call/tool-result pair (providers reject orphaned
  ``tool_result`` blocks): we walk back from the desired cut to the nearest
  ModelRequest that carries no ToolReturnPart.
- ``SystemPromptPart``s from the first message are re-attached to the summary
  message — otherwise compaction would silently drop the persona.
- The summary is injected as a clearly marked user-role message (synthetic
  system messages mid-history confuse some providers) followed by a short
  assistant acknowledgement, preserving role alternation.
- The summarization call goes through ``pydantic_ai.direct.model_request`` —
  a bare model call that does NOT recurse through this processor — and its
  token cost is added to the run's usage so Phase 3 limits stay honest.
- The summary is cached per old-segment fingerprint, so a REPL session only
  pays for re-summarization when the compacted region actually grows.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from pydantic_ai import RunContext
from pydantic_ai.direct import model_request
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from ..runtime.context import AgentDeps

logger = logging.getLogger("agent.compaction")

SUMMARY_MARK = "[conversation summary]"

_SUMMARY_PROMPT = (
    "Summarize the conversation transcript below for use as persistent context. "
    "Keep: stated facts (names, values, identifiers), decisions made, open tasks, "
    "and tool results that still matter. Drop pleasantries and dead ends. "
    "Be dense and factual.\n\n--- transcript ---\n"
)

_ACK = "Understood — I will continue with that context in mind."


# ── Size estimation & rendering ──────────────────────────────────────────────

def _part_text(part: Any) -> str:
    """Best-effort text of a message part (content, or tool-call args)."""
    content = getattr(part, "content", None)
    if content is not None:
        return content if isinstance(content, str) else str(content)
    args = getattr(part, "args", None)
    return str(args) if args is not None else ""


def estimate_tokens(messages: list[ModelMessage]) -> int:
    """Rough token count: total characters / 4. A trigger, not an invoice."""
    chars = sum(len(_part_text(p)) for m in messages for p in m.parts)
    return chars // 4


def _render(messages: list[ModelMessage]) -> str:
    """Plain-text transcript of *messages* for the summarization prompt."""
    lines: list[str] = []
    for m in messages:
        for p in m.parts:
            if isinstance(p, SystemPromptPart):
                continue  # persona is re-attached verbatim, not summarized
            text = _part_text(p).strip()
            if not text:
                continue
            if isinstance(p, UserPromptPart):
                role = "user"
            elif isinstance(p, ToolCallPart):
                role = f"tool call {p.tool_name}"
            elif isinstance(p, ToolReturnPart):
                role = f"tool result {p.tool_name}"
            else:
                role = "assistant"
            if len(text) > 2000:
                text = text[:2000] + "…(truncated)"
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


# ── Cut-point selection ──────────────────────────────────────────────────────

def _is_safe_boundary(msg: ModelMessage) -> bool:
    """A message the kept tail may START with: a request carrying no tool results.

    Cutting so the tail begins with a ToolReturnPart would orphan it from its
    ToolCallPart in the discarded ModelResponse — providers reject that.
    """
    return isinstance(msg, ModelRequest) and not any(
        isinstance(p, ToolReturnPart) for p in msg.parts
    )


def _find_cut(messages: list[ModelMessage], keep: int) -> int | None:
    """Largest safe index ≤ ``len - keep``, or None if no compaction is possible.

    Walking back (earlier) keeps more messages verbatim — safer than walking
    forward, which could cut into the recent context the model is acting on.
    Index 0 is never a cut (there would be nothing to compact).
    """
    desired = len(messages) - keep
    for i in range(min(desired, len(messages) - 1), 0, -1):
        if _is_safe_boundary(messages[i]):
            return i
    return None


# ── The processor ────────────────────────────────────────────────────────────

def build_history_processor(config: Any, model: Any) -> Callable | None:
    """Build the compaction history processor, or None when disabled.

    *config* is the loaded :class:`~agent.runtime.config.Config`; *model* is the
    already-built Pydantic AI model used for the (cheap, capped) summary call.
    """
    settings = config.settings
    comp = settings.get("compaction")
    comp = comp if isinstance(comp, dict) else {}
    if comp.get("enabled", True) is False:
        return None

    keep = int(comp.get("keep", 12))
    max_tokens = int(comp.get("max_tokens", 1024))
    budget = int(settings.get("context_budget", 100000))
    threshold = int(budget * 0.6)
    # Summary cache: old-segment fingerprint → injected message pair. A REPL
    # session re-summarizes only when the compacted region grows.
    cache: dict[tuple[int, int], list[ModelMessage]] = {}

    async def compact_history(
        ctx: RunContext[AgentDeps], messages: list[ModelMessage]
    ) -> list[ModelMessage]:
        if estimate_tokens(messages) <= threshold:
            return messages
        cut = _find_cut(messages, keep)
        if cut is None:  # e.g. one giant unfinished tool turn — nothing safe to do
            return messages
        old, tail = messages[:cut], messages[cut:]

        key = (len(old), sum(len(_part_text(p)) for m in old for p in m.parts))
        block = cache.get(key)
        if block is None:
            response = await model_request(
                model,
                [ModelRequest(parts=[UserPromptPart(content=_SUMMARY_PROMPT + _render(old))])],
                model_settings={"max_tokens": max_tokens},
            )
            summary = "".join(
                p.content for p in response.parts if isinstance(p, TextPart)
            ).strip()
            # Persona lives in the first message's SystemPromptPart(s) — carry
            # them onto the summary message or compaction would drop them.
            sys_parts = [p for p in old[0].parts if isinstance(p, SystemPromptPart)]
            block = [
                ModelRequest(parts=[*sys_parts, UserPromptPart(content=f"{SUMMARY_MARK}\n{summary}")]),
                ModelResponse(parts=[TextPart(content=_ACK)]),
            ]
            cache[key] = block
            try:  # the summary call costs tokens — keep Phase 3 limits honest
                ctx.usage.incr(response.usage)
            except Exception:  # noqa: BLE001 - usage accounting must never kill a run
                pass
            logger.info(
                "compacted %d messages (~%d tok) into a summary (~%d tok)",
                len(old), estimate_tokens(old), estimate_tokens(block),
            )
        return [*block, *tail]

    return compact_history
