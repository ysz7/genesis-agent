"""Phase 23a: scheduler core — interval parsing and the job store ops.

Pure store I/O over a real (temp) store; no agent, no network.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from agent.runtime import scheduler
from agent.runtime.store import open_store


# ── interval parsing ──────────────────────────────────────────────────────────

def test_parse_every_units():
    assert scheduler.parse_every("90") == 90            # bare = seconds
    assert scheduler.parse_every("30s") == 30
    assert scheduler.parse_every("5m") == 300
    assert scheduler.parse_every("2h") == 7200
    assert scheduler.parse_every("1d") == 86400
    assert scheduler.parse_every("every 5 minutes") == 300
    assert scheduler.parse_every("2 hours") == 7200


def test_parse_every_floor_and_errors():
    assert scheduler.parse_every("3") == scheduler.MIN_EVERY   # floored
    with pytest.raises(ValueError):
        scheduler.parse_every("soon")
    with pytest.raises(ValueError):
        scheduler.parse_every("5 fortnights")


def test_fmt_every():
    assert scheduler.fmt_every(86400) == "1d"
    assert scheduler.fmt_every(7200) == "2h"
    assert scheduler.fmt_every(300) == "5m"
    assert scheduler.fmt_every(45) == "45s"


# ── job store ─────────────────────────────────────────────────────────────────

def _store(tmp_path):
    return open_store(tmp_path / "state.json")


def test_add_and_list(tmp_path):
    store = _store(tmp_path)
    job = scheduler.add_job(store, "ping", 60, origin={"channel": "telegram", "user": "7"})
    assert job["id"] and job["every"] == 60 and job["deliver"] == "all"
    assert job["next_run"] > time.time()
    assert job["runs"] == 0 and job["origin"]["user"] == "7"
    assert [j["id"] for j in scheduler.list_jobs(store)] == [job["id"]]
    store.close()


def test_add_rejects_empty_and_respects_max(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        scheduler.add_job(store, "   ", 60)
    scheduler.add_job(store, "a", 60, max_jobs=2)
    scheduler.add_job(store, "b", 60, max_jobs=2)
    with pytest.raises(ValueError):
        scheduler.add_job(store, "c", 60, max_jobs=2)
    store.close()


def test_ids_are_unique(tmp_path):
    store = _store(tmp_path)
    ids = {scheduler.add_job(store, f"t{i}", 60)["id"] for i in range(20)}
    assert len(ids) == 20
    store.close()


def test_remove_and_edit(tmp_path):
    store = _store(tmp_path)
    job = scheduler.add_job(store, "ping", 60)
    assert scheduler.edit_job(store, job["id"], task="pong", every=120)["task"] == "pong"
    edited = scheduler.get_job(store, job["id"])
    assert edited["every"] == 120 and edited["task"] == "pong"
    assert scheduler.edit_job(store, "nope") is None
    assert scheduler.remove_job(store, job["id"]) is True
    assert scheduler.remove_job(store, job["id"]) is False
    assert scheduler.list_jobs(store) == []
    store.close()


def test_due_and_bump(tmp_path):
    store = _store(tmp_path)
    job = scheduler.add_job(store, "ping", 60)
    now = time.time()
    assert scheduler.due_jobs(store, now=now) == []          # not due yet
    assert len(scheduler.due_jobs(store, now=now + 61)) == 1  # past next_run
    scheduler.bump(store, job["id"], now=now + 61)
    after = scheduler.get_job(store, job["id"])
    assert after["runs"] == 1 and after["last_run"] == now + 61
    assert after["next_run"] == now + 61 + 60                # advanced one interval
    assert scheduler.due_jobs(store, now=now + 61) == []     # no longer due
    store.close()


# ── ownership lock (23c) ──────────────────────────────────────────────────────

def test_ownership_single_holder_and_stale_takeover(tmp_path):
    store = _store(tmp_path)
    t0 = 1000.0
    assert scheduler.claim_ownership(store, "A", ttl=60, now=t0) is True
    assert scheduler.claim_ownership(store, "B", ttl=60, now=t0 + 5) is False   # A still fresh
    assert scheduler.claim_ownership(store, "A", ttl=60, now=t0 + 5) is True    # A renews
    assert scheduler.claim_ownership(store, "B", ttl=60, now=t0 + 100) is True  # A went stale
    store.close()


# ── delivery fan-out (23d) ────────────────────────────────────────────────────

def test_delivery_all_channels_once_each(tmp_path):
    store = _store(tmp_path)
    job = scheduler.add_job(store, "ping", 60, deliver="all")
    scheduler.enqueue_delivery(store, job, "result text")
    assert len(scheduler.pending_for(store, "telegram")) == 1
    assert len(scheduler.pending_for(store, "cli")) == 1            # every channel sees it
    rec = scheduler.pending_for(store, "telegram")[0]
    scheduler.mark_delivered(store, rec["id"], "telegram")
    assert scheduler.pending_for(store, "telegram") == []          # consumed by telegram
    assert len(scheduler.pending_for(store, "cli")) == 1            # but cli still pending
    store.close()


def test_delivery_scoped_to_one_channel(tmp_path):
    store = _store(tmp_path)
    job = scheduler.add_job(store, "ping", 60, deliver="telegram")
    scheduler.enqueue_delivery(store, job, "x")
    assert len(scheduler.pending_for(store, "telegram")) == 1
    assert scheduler.pending_for(store, "cli") == []               # not addressed to cli
    store.close()


def test_delivery_independent_consumers(tmp_path):
    """Two consumers of one target (server-log drain vs /deliveries) don't race."""
    store = _store(tmp_path)
    job = scheduler.add_job(store, "ping", 60)
    rec = scheduler.enqueue_delivery(store, job, "x")
    # log drain consumes under its own key…
    assert len(scheduler.pending_for(store, "server", consumer="server-log")) == 1
    scheduler.mark_delivered(store, rec["id"], "server-log")
    assert scheduler.pending_for(store, "server", consumer="server-log") == []
    # …while the endpoint's key still sees the record once
    assert len(scheduler.pending_for(store, "server")) == 1
    scheduler.mark_delivered(store, rec["id"], "server")
    assert scheduler.pending_for(store, "server") == []
    store.close()


def test_delivery_ages_out(tmp_path):
    store = _store(tmp_path)
    job = scheduler.add_job(store, "ping", 60)
    rec = scheduler.enqueue_delivery(store, job, "x", now=0)        # ancient
    assert scheduler.pending_for(store, "telegram", now=99999) == []
    store.close()


# ── run_due_jobs (23c + 23d together) ─────────────────────────────────────────

class _FakeAgent:
    def __init__(self):
        self.ran = []
    async def run(self, task, deps=None, usage_limits=None):
        self.ran.append(task)
        return SimpleNamespace(output=f"did:{task}")


def _force_due(store, job_id):
    jobs = scheduler.list_jobs(store)
    for j in jobs:
        if j["id"] == job_id:
            j["next_run"] = 1
    store.set(scheduler.KEY, jobs)


def test_run_due_jobs_fires_bumps_enqueues(tmp_path):
    store = _store(tmp_path)
    job = scheduler.add_job(store, "ping", 60)
    _force_due(store, job["id"])
    agent = _FakeAgent()
    fired = asyncio.run(
        scheduler.run_due_jobs(store, agent, SimpleNamespace(), owner_id="me", ttl=60)
    )
    assert agent.ran == ["ping"]
    assert len(fired) == 1 and fired[0][1] == "did:ping" and fired[0][2] is True
    after = scheduler.get_job(store, job["id"])
    assert after["runs"] == 1 and after["next_run"] > time.time()
    assert len(scheduler.pending_for(store, "telegram")) == 1      # delivery enqueued
    store.close()


def test_run_due_jobs_noop_when_not_owner(tmp_path):
    store = _store(tmp_path)
    job = scheduler.add_job(store, "ping", 60)
    _force_due(store, job["id"])
    scheduler.claim_ownership(store, "other", ttl=600)             # someone else owns
    agent = _FakeAgent()
    fired = asyncio.run(
        scheduler.run_due_jobs(store, agent, SimpleNamespace(), owner_id="me", ttl=600)
    )
    assert fired == [] and agent.ran == []
    store.close()
