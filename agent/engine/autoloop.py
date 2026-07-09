"""Todo-driven autonomous continuation — the plan as a soft control loop (Phase 35).

By default the planning scratchpad (Phase 13) is passive: the model keeps a
checklist, but a run stops after one model turn even if steps remain ``pending``.
With ``planning: {autoloop: true}`` the shared event walk (``engine/runner.py``)
re-enters the agent — feeding a synthetic "continue with the plan" nudge and the
prior history — until every step is ``done`` or a hard cap is hit.

Settings (``settings.yaml``)::

    planning:
      enabled: true
      autoloop: false        # off by default (autonomy risk — a vertical opt-in)
      max_iterations: 8       # hard cap on total agent passes (1 = no continuation)

Guardrails (why this can't run away):

- **Hard iteration cap.** ``max_iterations`` bounds the total number of passes;
  a plan that never completes stops there, not in an infinite loop.
- **Aggregate budget.** The runner threads one shared ``RunUsage`` across passes,
  so the existing ``usage_limits`` / ``request_limit`` bound the *whole* loop, not
  each pass in isolation — the same backstop the verify loop uses.
- **Early exit.** Each pass re-checks the plan, so a model that finishes early (or
  clears the plan) leaves immediately rather than burning the remaining budget.

Never on by default. Composes with verify (Phase 31): autoloop drives the plan to
completion inside the walk; verify judges the final answer after it.
"""

from __future__ import annotations

from ..runtime.context import AgentDeps

_NUDGE = (
    "Continue with your plan — not every step is done yet. Current checklist:\n\n"
    "{plan}\n\n"
    "Do the next unfinished step now and keep the plan updated with update_plan. "
    "When every step is done, give your final answer. If a step genuinely can't be "
    "completed, mark it done or revise the plan and say why — don't loop on it."
)


def _block(settings: dict) -> dict:
    b = settings.get("planning")
    return b if isinstance(b, dict) else {}


def max_iterations(settings: dict) -> int:
    """Hard cap on total agent passes (min 1 — a lone pass = no continuation)."""
    try:
        return max(1, int(_block(settings).get("max_iterations", 8)))
    except (TypeError, ValueError):
        return 8


def autoloop_enabled(settings: dict) -> bool:
    """True when planning + autoloop are on and the cap allows more than one pass."""
    b = _block(settings)
    return bool(b.get("enabled")) and bool(b.get("autoloop")) and max_iterations(settings) > 1


def plan_has_open_steps(deps: AgentDeps) -> bool:
    """True when a plan exists in this run and any step isn't ``done`` yet."""
    plan = deps.extra.get("plan")
    if not plan:
        return False
    return any(s.get("status") != "done" for s in plan)


def build_nudge(deps: AgentDeps) -> str:
    """The synthetic 'continue with the plan' follow-up, showing the live checklist."""
    from ..tools.planning import render_plan

    return _NUDGE.format(plan=render_plan(deps.extra.get("plan") or []))
