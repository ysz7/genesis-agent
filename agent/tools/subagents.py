"""Subagent delegation (Phase 14) — opt-in.

``delegate(task)`` runs a fresh sub-agent on an isolated subtask (clean message
history) and returns just its final answer, so the parent's context stays lean.

Safety, all on by default:
- **Depth guard** (`subagents.max_depth`, default 1): a sub-agent at the limit
  has no ``delegate`` tool at all *and* the tool refuses — no fork bombs.
- **Per-subagent usage limits**: each sub-agent runs under the same
  `UsageLimits` as the parent, and its token cost is folded back into the
  parent run so Phase 3 budgets stay honest.
- **Restricted toolset**: sub-agents never get ``write_tool`` (no self-modifying
  code from a delegated context).

Sub-agents share ``deps`` (store / http / workspace) but NOT message history —
isolation of *context*, not of *state*.
"""

from __future__ import annotations

import dataclasses

from pydantic_ai import RunContext

from ..runtime.context import AgentDeps

# Tools a sub-agent never receives, regardless of depth.
_ALWAYS_EXCLUDE = {"write_tool"}


def delegate(ctx: RunContext[AgentDeps], task: str) -> str:
    """Delegate a self-contained subtask to a fresh sub-agent; return its answer.

    The sub-agent starts with NO memory of this conversation — it sees only
    ``task`` — so include all the context it needs. Use this to keep your own
    context clean: focused lookups, isolated multi-step subtasks, or splitting a
    job into parts (call it several times). You get back only the final answer.

    Args:
        task: A complete, standalone instruction for the sub-agent.
    """
    from ..engine.factory import build_agent  # local import avoids an import cycle

    deps = ctx.deps
    settings = deps.settings.get("subagents") or {}
    max_depth = int(settings.get("max_depth", 1))
    depth = int(deps.extra.get("delegation_depth", 0))
    if depth >= max_depth:
        return (
            f"Refused: delegation depth limit ({max_depth}) reached — this "
            f"sub-agent cannot delegate further."
        )

    # Restricted toolset: never self-modify; drop `delegate` once the child would
    # be at the depth limit, so it can't recurse.
    exclude = set(_ALWAYS_EXCLUDE)
    if depth + 1 >= max_depth:
        exclude.add("delegate")

    sub_agent = build_agent(deps.config, exclude_tools=exclude)
    # Isolated context = fresh run with no message_history. Share state (store,
    # http, workspace, hooks); give a fresh `extra` carrying the new depth so a
    # parent's plan / depth don't leak in and concurrent calls don't collide.
    sub_deps = dataclasses.replace(deps, extra={"delegation_depth": depth + 1})

    try:
        result = sub_agent.run_sync(
            task, deps=sub_deps, usage_limits=deps.config.usage_limits
        )
    except Exception as exc:  # noqa: BLE001 - surface the sub-agent's failure
        return f"Sub-agent error: {exc}"

    # Fold the sub-agent's cost into this run so usage limits stay honest.
    try:
        usage = result.usage
        ctx.usage.incr(usage if hasattr(usage, "input_tokens") else usage())
    except Exception:  # noqa: BLE001 - usage accounting must never break a run
        pass
    return str(result.output)


#: Registered when ``subagents.enabled`` is true.
SUBAGENT_TOOLS = [delegate]
