"""Phase 23b: agent-facing scheduling tools.

Let the agent create, list, edit and cancel recurring tasks from a normal
conversation (CLI or a gateway). Jobs live in the shared store (see
``runtime/scheduler``) and fire in the background while a long-lived process (the
server or a gateway bot) is up; results are delivered to all channels. These are
opt-in — registered only when ``scheduler.enabled`` (on by default).

A tool reaches the store / settings via ``ctx.deps``; the entrypoint stamps
``ctx.deps.extra["channel_origin"]`` so a scheduled job remembers where it was
created (used for context, and as one of the delivery targets).
"""

from __future__ import annotations

import time

from pydantic_ai import RunContext

from ..runtime.context import AgentDeps
from ..runtime import scheduler


def _max_jobs(ctx: RunContext[AgentDeps]) -> int | None:
    return (ctx.deps.settings.get("scheduler") or {}).get("max_jobs")


def schedule_task(ctx: RunContext[AgentDeps], task: str, every: str) -> str:
    """Schedule a task to run repeatedly on an interval.

    The task is a natural-language instruction you'll run each time it fires (e.g.
    "summarize the Hacker News front page"). It runs in the background while the
    server or a gateway bot is up, and the result is delivered to all channels.

    Args:
        task: What to do each time (a self-contained instruction).
        every: Interval, e.g. "30s", "5m", "2h", "1d" (or "every 2 hours").
    """
    try:
        seconds = scheduler.parse_every(every)
    except ValueError as exc:
        return f"Couldn't schedule: {exc}"
    try:
        job = scheduler.add_job(
            ctx.deps.store, task, seconds,
            origin=ctx.deps.extra.get("channel_origin"),
            max_jobs=_max_jobs(ctx),
        )
    except ValueError as exc:
        return f"Couldn't schedule: {exc}"
    return (
        f"Scheduled (id {job['id']}): every {scheduler.fmt_every(seconds)} — {job['task']}. "
        f"It runs while the server or a bot is up; results go to all channels."
    )


def list_scheduled(ctx: RunContext[AgentDeps]) -> str:
    """List all scheduled recurring tasks (id, interval, when next, task)."""
    jobs = scheduler.list_jobs(ctx.deps.store)
    if not jobs:
        return "No scheduled tasks."
    now = time.time()
    lines = []
    for job in jobs:
        due_in = int(float(job.get("next_run", now)) - now)
        when = f"in {scheduler.fmt_every(max(0, due_in))}" if due_in > 0 else "due now"
        lines.append(
            f"[{job['id']}] every {scheduler.fmt_every(job['every'])} · next {when} · "
            f"runs {job.get('runs', 0)}\n    {job['task']}"
        )
    return "\n".join(lines)


def cancel_scheduled(ctx: RunContext[AgentDeps], job_id: str) -> str:
    """Cancel (delete) a scheduled task by its id (from list_scheduled)."""
    ok = scheduler.remove_job(ctx.deps.store, job_id)
    return f"Cancelled task {job_id}." if ok else f"No scheduled task with id {job_id}."


def edit_scheduled(
    ctx: RunContext[AgentDeps],
    job_id: str,
    task: str | None = None,
    every: str | None = None,
) -> str:
    """Edit a scheduled task's instruction and/or its interval.

    Args:
        job_id: The task id (from list_scheduled).
        task: New instruction (omit to keep the current one).
        every: New interval, e.g. "10m", "1h" (omit to keep the current one).
    """
    seconds = None
    if every is not None:
        try:
            seconds = scheduler.parse_every(every)
        except ValueError as exc:
            return f"Couldn't edit: {exc}"
    job = scheduler.edit_job(ctx.deps.store, job_id, task=task, every=seconds)
    if job is None:
        return f"No scheduled task with id {job_id}."
    return f"Updated task {job_id}: every {scheduler.fmt_every(job['every'])} — {job['task']}"


#: The scheduling tools, registered when ``scheduler.enabled``.
SCHEDULING_TOOLS = [schedule_task, list_scheduled, cancel_scheduled, edit_scheduled]
