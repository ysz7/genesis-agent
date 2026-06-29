"""Phase 23: recurring scheduled tasks — the shared job store + helpers.

One list of jobs lives in the state store under ``scheduled_jobs`` and is the
single source of truth for the agent's scheduling tools (``tools/scheduling.py``),
the background ticker (gateway · server), and the menu's live scheduler. Each job
is a plain dict::

    {"id": "a1b2",            # short stable id (list / cancel / edit)
     "task": "summarize HN",  # the prompt run on each fire
     "every": 3600,           # interval in seconds
     "next_run": 1719600000,  # absolute epoch — survives restarts, no drift
     "deliver": "all",        # "all" (every channel) | channel name | None (log)
     "origin": {"channel": "telegram", "user": "411201608"},
     "created": ..., "last_run": ..., "runs": 0}

This module is pure store I/O + parsing — no agent, no network — so it unit-tests
trivially. Ownership (single-runner lock) and delivery fan-out build on the keys
defined here in later sub-phases.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import socket
import time
from typing import Any

logger = logging.getLogger("agent.scheduler")

KEY = "scheduled_jobs"            # the job list (shared with the menu)
OWNER_KEY = "scheduler:owner"     # single-runner heartbeat lock (Phase 23c)
DELIVERIES_KEY = "scheduler:deliveries"  # pending delivery records (Phase 23d)

MIN_EVERY = 10                    # floor on an interval (seconds) — avoid runaway

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def parse_every(text: Any) -> int:
    """Parse a human interval (``"30s"``, ``"5m"``, ``"2 hours"``, ``"90"``) to seconds.

    A bare number is seconds. Raises ``ValueError`` on anything unparseable. The
    result is floored at :data:`MIN_EVERY` so a recurring task can't hammer.
    """
    t = str(text).strip().lower().replace("every", "").strip()
    m = re.fullmatch(r"(\d+)\s*([a-z]*)", t)
    if not m:
        raise ValueError(f"can't parse interval {text!r}; use e.g. '30s', '5m', '2h', '1d'")
    num, unit = int(m.group(1)), (m.group(2) or "s")
    if unit not in _UNITS:
        raise ValueError(f"unknown time unit {unit!r}; use s / m / h / d")
    return max(MIN_EVERY, num * _UNITS[unit])


def fmt_every(seconds: Any) -> str:
    """Render a seconds interval compactly: ``3600 -> "1h"``."""
    s = int(seconds)
    if s and s % 86400 == 0:
        return f"{s // 86400}d"
    if s and s % 3600 == 0:
        return f"{s // 3600}h"
    if s and s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


# ── job store ────────────────────────────────────────────────────────────────

def list_jobs(store: Any) -> list[dict]:
    jobs = store.get(KEY, []) or []
    return jobs if isinstance(jobs, list) else []


def get_job(store: Any, job_id: str) -> dict | None:
    for job in list_jobs(store):
        if job.get("id") == job_id:
            return job
    return None


def _new_id(jobs: list[dict]) -> str:
    existing = {j.get("id") for j in jobs}
    while True:
        jid = secrets.token_hex(2)        # 4 hex chars — short, easy to type back
        if jid not in existing:
            return jid


def add_job(
    store: Any,
    task: str,
    every: int,
    *,
    deliver: Any = "all",
    origin: dict | None = None,
    max_jobs: int | None = None,
) -> dict:
    """Append a new job (first fire one interval from now). Raises on bad input."""
    task = str(task).strip()
    if not task:
        raise ValueError("task is empty")
    every = int(every)
    jobs = list_jobs(store)
    if max_jobs and len(jobs) >= int(max_jobs):
        raise ValueError(f"too many scheduled jobs (max {max_jobs}); cancel one first")
    now = time.time()
    job = {
        "id": _new_id(jobs), "task": task, "every": every,
        "next_run": now + every, "deliver": deliver, "origin": origin,
        "created": now, "last_run": None, "runs": 0,
    }
    jobs.append(job)
    store.set(KEY, jobs)
    return job


def remove_job(store: Any, job_id: str) -> bool:
    jobs = list_jobs(store)
    kept = [j for j in jobs if j.get("id") != job_id]
    if len(kept) == len(jobs):
        return False
    store.set(KEY, kept)
    return True


def edit_job(store: Any, job_id: str, *, task: str | None = None, every: int | None = None) -> dict | None:
    """Update a job's task and/or interval (resets ``next_run`` when interval changes)."""
    jobs = list_jobs(store)
    found: dict | None = None
    for job in jobs:
        if job.get("id") == job_id:
            if task is not None:
                job["task"] = str(task).strip()
            if every is not None:
                job["every"] = int(every)
                job["next_run"] = time.time() + int(every)
            found = job
    if found is not None:
        store.set(KEY, jobs)
    return found


def due_jobs(store: Any, now: float | None = None) -> list[dict]:
    """Jobs whose ``next_run`` is in the past (ready to fire)."""
    now = time.time() if now is None else now
    return [j for j in list_jobs(store) if float(j.get("next_run", 0)) <= now]


def bump(store: Any, job_id: str, now: float | None = None) -> None:
    """Mark a job as just run: advance ``next_run``, set ``last_run``, count it."""
    now = time.time() if now is None else now
    jobs = list_jobs(store)
    for job in jobs:
        if job.get("id") == job_id:
            job["next_run"] = now + int(job.get("every", 0))
            job["last_run"] = now
            job["runs"] = int(job.get("runs", 0)) + 1
    store.set(KEY, jobs)


# ── single-runner ownership lock (Phase 23c) ──────────────────────────────────
# Exactly one long-lived process executes due jobs at a time, so a gateway and the
# server don't double-fire. A heartbeat record names the current owner; a stale
# heartbeat (older than ttl) is taken over. Delivery (23d) is separate, so the one
# runner executes once and every channel still gets the result.

def default_owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def claim_ownership(store: Any, owner_id: str, ttl: float, now: float | None = None) -> bool:
    """True if *owner_id* holds (or just took) the runner lock.

    Backs off only to a *fresh* lock held by someone else; a stale or absent lock
    is claimed. After writing we re-read and confirm we won, so two racing
    claimants converge to a single owner within a tick.
    """
    now = time.time() if now is None else now
    cur = store.get(OWNER_KEY)
    if isinstance(cur, dict) and cur.get("id") != owner_id and (now - float(cur.get("ts", 0))) < ttl:
        return False
    store.set(OWNER_KEY, {"id": owner_id, "ts": now})
    cur = store.get(OWNER_KEY)
    return isinstance(cur, dict) and cur.get("id") == owner_id


# ── multi-channel delivery fan-out (Phase 23d) ────────────────────────────────
# When a job fires, the runner enqueues a delivery record; each running channel
# drains the records addressed to it (deliver "all" or its own name) and sends to
# its recipients, marking itself consumed so it delivers each at most once. Records
# age out (no need to know the full set of channels that are currently up).

_DELIVERY_TTL = 3600.0        # drop undelivered records after an hour
_DELIVERY_CAP = 200           # keep the list bounded


def enqueue_delivery(store: Any, job: dict, text: str, now: float | None = None) -> dict:
    """Record a fired job's result for channels to deliver (skips ``deliver: None``)."""
    now = time.time() if now is None else now
    rec = {
        "id": secrets.token_hex(3),
        "job_id": job.get("id"),
        "text": text,
        "deliver": job.get("deliver", "all"),
        "origin": job.get("origin"),
        "ts": now,
        "consumed": [],
    }
    records = store.get(DELIVERIES_KEY, []) or []
    records.append(rec)
    store.set(DELIVERIES_KEY, records[-_DELIVERY_CAP:])
    return rec


def pending_for(store: Any, channel: str, now: float | None = None) -> list[dict]:
    """Delivery records *channel* should send (target all/itself, not yet consumed)."""
    now = time.time() if now is None else now
    out = []
    for rec in store.get(DELIVERIES_KEY, []) or []:
        if (now - float(rec.get("ts", 0))) > _DELIVERY_TTL:
            continue
        if rec.get("deliver", "all") not in ("all", channel):
            continue
        if channel in rec.get("consumed", []):
            continue
        out.append(rec)
    return out


def mark_delivered(store: Any, delivery_id: str, channel: str, now: float | None = None) -> None:
    """Mark one record consumed by *channel*; age-purge old records."""
    now = time.time() if now is None else now
    records = store.get(DELIVERIES_KEY, []) or []
    kept = []
    for rec in records:
        if rec.get("id") == delivery_id and channel not in rec.get("consumed", []):
            rec.setdefault("consumed", []).append(channel)
        if (now - float(rec.get("ts", 0))) <= _DELIVERY_TTL:
            kept.append(rec)
    store.set(DELIVERIES_KEY, kept)


def _result_text(result: Any) -> str:
    out = getattr(result, "output", result)
    return out if isinstance(out, str) else str(out)


async def run_due_jobs(
    store: Any,
    agent: Any,
    deps: Any,
    *,
    owner_id: str,
    ttl: float,
    usage_limits: Any = None,
    on_fire=None,
) -> list[tuple[dict, str, bool]]:
    """If we own the runner lock, run every due job once and enqueue its delivery.

    Returns ``[(job, text, ok), …]`` for the jobs that fired (empty if not owner).
    Each run is stateless (no per-user thread). ``on_fire(job, text, ok)`` is an
    optional callback for logging/monitoring.
    """
    if not claim_ownership(store, owner_id, ttl):
        return []
    fired: list[tuple[dict, str, bool]] = []
    for job in due_jobs(store):
        ok = True
        try:
            result = await agent.run(job["task"], deps=deps, usage_limits=usage_limits)
            text = _result_text(result)
        except Exception as exc:  # noqa: BLE001 - a bad task must not kill the ticker
            logger.warning("scheduled task %s failed: %s", job.get("id"), exc)
            text = f"Scheduled task '{job.get('task')}' failed: {exc}"
            ok = False
        bump(store, job["id"])
        if job.get("deliver") is not None:
            enqueue_delivery(store, job, text)
        if on_fire is not None:
            on_fire(job, text, ok)
        fired.append((job, text, ok))
    return fired
