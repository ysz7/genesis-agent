"""Verify / self-critique loop — the evaluator–optimizer pattern (Phase 31).

Before finalizing, an optional reflection pass judges the answer against the task
and revises once if it falls short. The evaluator is a **separate actor**: a
bounded ``pydantic_ai.direct.model_request`` run with fresh context (like
compaction's summary call — it does NOT recurse the agent loop), so the model
grading the work isn't the same context that produced it. On a fail verdict the
run is repeated with the evaluator's ``weakest`` + ``gaps`` fed back as a nudge,
capped at ``max_rounds``.

Settings (``settings.yaml``)::

    verify:
      enabled: false          # off by default (costs an extra call)
      max_rounds: 1           # revisions after the first answer (0 disables)
      # model: gpt-4.1-mini   # optional cheaper evaluator (same provider/key)
      # min_output_chars: 400 # scope guard: skip verifying tiny one-shot answers

Design (the hardened form of the community "loop" prompt):

- **Separate actor from critic.** The evaluator gets fresh context and, ideally, a
  cheaper model — no in-context self-scoring (optimistic and gameable).
- **Structural stop, no magic threshold.** Stop on ``ok == true`` OR ``max_rounds``
  reached — never a self-declared numeric score.
- **Fix the weakest first.** The optimizer re-run is fed only ``weakest`` + ``gaps``.
- **No ceremony in history.** The evaluation runs off to the side; only its verdict
  (as a nudge) re-enters the loop.
- **Honest usage.** First-run cost, every evaluator call, and every re-run are
  folded into one accumulating ``RunUsage`` that the returned result reports.
- **Fail-open.** A flaky evaluator (unparseable verdict, a raised re-run) never
  blocks — the current best answer is returned.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from typing import Any, Callable

from pydantic_ai import Agent
from pydantic_ai.direct import model_request
from pydantic_ai.messages import (
    ModelRequest,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from ..runtime.context import AgentDeps

logger = logging.getLogger("agent.verify")

_EVAL_PROMPT = """You are a strict evaluator. You did NOT write the answer below \
— judge it with fresh eyes against the task and criteria. Do not be generous.

TASK:
{task}

CRITERIA (all must be met):
{criteria}

ANSWER:
{output}

Return JSON only, no prose:
{{"ok": <true only if every criterion is fully met>,
  "weakest": "<the single criterion least satisfied>",
  "gaps": "<concrete, actionable — what to change, not a score>"}}"""

_DERIVE_CRITERIA = (
    "Derive the criteria yourself from the task: what would a careful reviewer "
    "insist on for this specific request to count as fully done?"
)

_NUDGE = (
    "Your previous answer did not fully meet the task. Fix the single weakest "
    "point first, then tighten the rest.\n\n"
    "Weakest: {weakest}\n"
    "What to fix: {gaps}\n\n"
    "Provide the improved answer."
)


@dataclasses.dataclass
class _Verdict:
    ok: bool
    weakest: str
    gaps: str


def verification_enabled(settings: dict) -> bool:
    """True when ``verify: {enabled: true}`` with a positive ``max_rounds``."""
    v = settings.get("verify")
    v = v if isinstance(v, dict) else {}
    return bool(v.get("enabled")) and int(v.get("max_rounds", 1)) > 0


def _made_tool_calls(result: Any) -> bool:
    try:
        return any(
            isinstance(p, ToolCallPart)
            for m in result.all_messages()
            for p in m.parts
        )
    except Exception:  # noqa: BLE001 - never let the scope check kill a run
        return True  # unknown → treat as non-trivial (safer to verify)


def _is_trivial(result: Any, min_chars: int) -> bool:
    """Skip verifying a small one-shot answer: no tool calls AND short output.

    Honours the persona's "match effort to the ask" — a two-line reply shouldn't
    pay for an evaluator round.
    """
    if not isinstance(result.output, str):
        return True  # structured output is already typed/validated — nothing to grade
    return len(result.output) < min_chars and not _made_tool_calls(result)


def _parse_verdict(text: str) -> _Verdict:
    """Lenient JSON extraction. Unparseable → ``ok`` (fail-open, never loop blind)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return _Verdict(True, "", "")
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return _Verdict(True, "", "")
    return _Verdict(
        ok=bool(data.get("ok", True)),
        weakest=str(data.get("weakest", "")).strip(),
        gaps=str(data.get("gaps", "")).strip(),
    )


async def _evaluate(
    model: Any, task: str, output: str, criteria: str, max_tokens: int
) -> tuple[_Verdict, RunUsage]:
    prompt = _EVAL_PROMPT.format(task=task, criteria=criteria, output=output)
    response = await model_request(
        model,
        [ModelRequest(parts=[UserPromptPart(content=prompt)])],
        model_settings={"max_tokens": max_tokens},
    )
    text = "".join(p.content for p in response.parts if isinstance(p, TextPart))
    return _parse_verdict(text), response.usage


async def verify_and_revise(
    agent: Agent,
    task: str,
    deps: AgentDeps,
    result: Any,
    *,
    notify: Callable[[str], None] | None = None,
) -> Any:
    """Judge *result* against *task*; revise once per fail, up to ``max_rounds``.

    Returns the final result (the original when disabled, out of scope, or already
    good). ``notify(weakest)`` — if given — is called just before each revision so
    the CLI can surface a "revising" line. Usage across the first run, every
    evaluator call, and every re-run is folded into the returned result's usage
    (via a preloaded, accumulating :class:`RunUsage`).
    """
    settings = deps.settings
    if not verification_enabled(settings):
        return result
    v = settings.get("verify") or {}
    max_rounds = int(v.get("max_rounds", 1))
    min_chars = int(v.get("min_output_chars", 400))
    if _is_trivial(result, min_chars):
        return result

    # Cheaper evaluator model (same provider/key) if configured, else the agent's.
    model = agent.model
    override = v.get("model")
    cfg = getattr(deps, "config", None)
    if override and cfg is not None:
        from .model import build_model

        model = build_model(dataclasses.replace(cfg, model=str(override)))

    criteria = _DERIVE_CRITERIA
    max_tokens = int(v.get("max_tokens", 400))

    # One accumulating usage: preload the first run so the returned result's usage
    # stays honest (re-runs pass usage=shared → their result.usage is the total).
    shared = RunUsage()
    try:
        shared.incr(result.usage)
    except Exception:  # noqa: BLE001 - usage accounting must never kill a run
        pass

    for _ in range(max_rounds):
        try:
            verdict, eval_usage = await _evaluate(
                model, task, str(result.output), criteria, max_tokens
            )
            shared.incr(eval_usage)
        except Exception as exc:  # noqa: BLE001 - a failed critique returns the answer as-is
            logger.debug("verify: evaluator failed, keeping current answer (%s)", exc)
            return result
        if verdict.ok:
            break
        if notify is not None:
            notify(verdict.weakest or "the answer")
        logger.info("verify: revising (weakest: %s)", verdict.weakest or "?")
        nudge = _NUDGE.format(weakest=verdict.weakest, gaps=verdict.gaps)
        usage_limits = getattr(getattr(deps, "config", None), "usage_limits", None)
        try:
            result = await agent.run(
                nudge,
                deps=deps,
                message_history=result.all_messages(),
                usage_limits=usage_limits,
                usage=shared,
            )
        except Exception as exc:  # noqa: BLE001 - out of budget / transient → keep best
            logger.info("verify: re-run stopped (%s); returning best answer", exc)
            break
    return result


async def run_then_verify(
    agent: Agent,
    task: str,
    deps: AgentDeps,
    *,
    message_history: Any = None,
    notify: Callable[[str], None] | None = None,
) -> Any:
    """Convenience for the ``agent.run`` call sites: run once, then verify.

    Streaming sites (CLI tree, server SSE) already own the first run via
    ``iter_events`` and call :func:`verify_and_revise` directly on its result.
    """
    usage_limits = getattr(getattr(deps, "config", None), "usage_limits", None)
    result = await agent.run(
        task,
        deps=deps,
        message_history=message_history,
        usage_limits=usage_limits,
    )
    return await verify_and_revise(agent, task, deps, result, notify=notify)
