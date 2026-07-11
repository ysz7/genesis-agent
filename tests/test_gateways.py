"""Phase 22a: gateway scaffold — pipeline, access control, store guard, registry.

No network and no real model: a fake agent records what history it was handed, so
per-user thread loading is observable, and the access/store helpers are unit-pure.
"""

import asyncio
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import (
    ModelRequest, ModelResponse, UserPromptPart, TextPart,
)

from agent.gateways.base import (
    AccessControl, Gateway, Inbound, Pipeline, Quota,
    any_gateway_enabled, gateway_enabled, gateway_settings, store_guard,
)
from agent.gateways import manager, registry
from agent.runtime import threads
from agent.runtime.store import open_store


# ── fakes ─────────────────────────────────────────────────────────────────────

def _msgs():
    return [
        ModelRequest(parts=[UserPromptPart(content="prior turn")]),
        ModelResponse(parts=[TextPart(content="ok")]),
    ]


class _Result:
    def __init__(self, output):
        self.output = output
    def all_messages(self):
        return _msgs()


class _FakeAgent:
    """Records the message_history of each run so thread loading is observable."""
    def __init__(self):
        self.histories = []
    async def run(self, prompt, deps=None, message_history=None, usage_limits=None):
        self.histories.append(message_history)
        return _Result(f"echo:{prompt}")


def _deps(store):
    return SimpleNamespace(store=store, settings={})


# ── settings helpers ──────────────────────────────────────────────────────────

def test_gateway_settings_and_enabled():
    s = {"gateways": {"telegram": {"enabled": True, "allowlist": [1, 2]}}}
    assert gateway_settings(s, "telegram")["allowlist"] == [1, 2]
    assert gateway_enabled(s, "telegram") is True
    assert gateway_enabled(s, "whatsapp") is False
    assert any_gateway_enabled(s) is True
    assert any_gateway_enabled({"gateways": {"telegram": {"enabled": False}}}) is False
    assert gateway_settings({}, "telegram") == {}


# ── concurrent-store guard ────────────────────────────────────────────────────

def test_store_guard_rejects_json_accepts_sqlite(tmp_path):
    json_store = open_store(tmp_path / "state.json")
    assert store_guard(json_store) is not None      # JSON is unsafe → message
    json_store.close()
    sql_store = open_store(tmp_path / "agent.sqlite")
    assert store_guard(sql_store) is None            # SQLite/WAL is fine
    sql_store.close()


# ── access control (deny-all) ─────────────────────────────────────────────────

def test_access_deny_all_when_empty(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    ac = AccessControl(store, "telegram", seed=None)
    assert ac.allowed("123") is False                # empty allowlist = nobody
    assert ac.listing() == []
    store.close()


def test_access_allow_and_deny_persist(tmp_path):
    path = tmp_path / "agent.sqlite"
    store = open_store(path)
    ac = AccessControl(store, "telegram", seed=None)
    ac.allow(123)
    assert ac.allowed("123") is True                 # ids are string-keyed
    store.close()
    # "restart": a fresh store over the same file keeps the grant
    store2 = open_store(path)
    ac2 = AccessControl(store2, "telegram", seed=None)
    assert ac2.allowed(123) is True
    assert ac2.deny(123) is True
    assert ac2.allowed(123) is False
    store2.close()


def test_access_seed_from_settings(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    ac = AccessControl(store, "telegram", seed=[42])
    assert ac.allowed(42) is True                    # seeded id is allowed
    assert ac.deny(42) is False                       # but a seed can't be revoked live
    store.close()


# ── quota (22i) ───────────────────────────────────────────────────────────────

def test_quota_limits_per_day(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    q = Quota(store, "telegram", 2)
    assert q.allowed("7")
    q.increment("7")
    assert q.allowed("7")
    q.increment("7")
    assert q.allowed("7") is False                    # cap reached
    assert q.allowed("8") is True                     # other users are independent
    store.close()


def test_quota_unlimited_when_zero(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    q = Quota(store, "telegram", 0)
    for _ in range(5):
        q.increment("7")
    assert q.allowed("7") is True                     # 0 = unlimited
    store.close()


# ── pipeline ──────────────────────────────────────────────────────────────────

def test_pipeline_session_mapping():
    pipe = Pipeline("telegram", _FakeAgent(), _deps(None), {})
    assert pipe.session_for(999) == "telegram:999"


def test_pipeline_runs_and_threads_per_user(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    agent = _FakeAgent()
    pipe = Pipeline("telegram", agent, _deps(store), {})
    out1 = asyncio.run(pipe.run_turn(Inbound(user_id="7", text="hi")))
    asyncio.run(pipe.run_turn(Inbound(user_id="7", text="again")))  # 2nd turn: build history
    assert out1 == "echo:hi"
    assert agent.histories[0] is None                # first turn: no prior history
    assert agent.histories[1] is not None            # second turn: thread reloaded
    assert len(agent.histories[1]) == 2
    # a different user has an independent thread
    asyncio.run(pipe.run_turn(Inbound(user_id="8", text="hello")))
    assert agent.histories[2] is None
    store.close()


def test_pipeline_input_guardrail_short_circuits(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    agent = _FakeAgent()
    settings = {"guardrails": {"input": {"block": ["(?i)secret"]}}}
    pipe = Pipeline("telegram", agent, _deps(store), settings)
    out = asyncio.run(pipe.run_turn(Inbound(user_id="7", text="the secret code")))
    assert "Refused" in out
    assert agent.histories == []                      # agent never ran
    store.close()


# ── Phase 39: gateway per-user sessions ───────────────────────────────────────

class _EchoResult:
    """A result whose saved history carries the prompt, so we can tell which
    session a message actually landed in."""
    def __init__(self, prompt):
        self.output = f"echo:{prompt}"
        self._p = str(prompt)
        self.usage = None
    def all_messages(self):
        return [
            ModelRequest(parts=[UserPromptPart(content=self._p)]),
            ModelResponse(parts=[TextPart(content=f"echo:{self._p}")]),
        ]


class _EchoAgent:
    async def run(self, prompt, deps=None, message_history=None, usage_limits=None):
        return _EchoResult(prompt)


def _sess_deps(store, settings):
    return SimpleNamespace(
        store=store, settings=settings,
        config=SimpleNamespace(settings=settings, usage_limits=None),
        extra={},
    )


def test_pipeline_lands_messages_in_active_session(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    settings = {"threads": {"enabled": True, "autotitle": "off"}}
    pipe = Pipeline("telegram", _EchoAgent(), _sess_deps(store, settings), settings)

    # First message → default 'main' session.
    asyncio.run(pipe.run_turn(Inbound(user_id="7", text="hello main")))
    assert threads.active_slug(store, "telegram:7") == "main"
    assert "hello main" in str(threads.load_thread(store, "telegram:7:main"))

    # Switch to a new session; the next message lands there, not in main.
    threads.new_session(store, "telegram:7", "work", channel="telegram")
    asyncio.run(pipe.run_turn(Inbound(user_id="7", text="hello work")))
    assert "hello work" in str(threads.load_thread(store, "telegram:7:work"))
    assert "hello work" not in str(threads.load_thread(store, "telegram:7:main"))  # isolated

    # Resume main; new messages land back there.
    threads.set_active(store, "telegram:7", "main")
    asyncio.run(pipe.run_turn(Inbound(user_id="7", text="back in main")))
    assert "back in main" in str(threads.load_thread(store, "telegram:7:main"))
    assert "back in main" not in str(threads.load_thread(store, "telegram:7:work"))
    store.close()


def test_pipeline_single_thread_when_threads_off(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    pipe = Pipeline("telegram", _EchoAgent(), _sess_deps(store, {}), {})
    assert pipe.session_for("7") == "telegram:7"                 # no slug, today's id
    asyncio.run(pipe.run_turn(Inbound(user_id="7", text="hi")))
    assert threads.active_slug(store, "telegram:7") is None      # no active pointer created
    assert "hi" in str(threads.load_thread(store, "telegram:7"))
    store.close()


class _Gw(Gateway):
    name = "telegram"
    async def run(self):  # abstract, unused in these tests
        pass
    async def send_message(self, client, recipient, text):
        pass


def _gw(store, settings):
    config = SimpleNamespace(settings=settings, model="m", usage_limits=None)
    return _Gw(config, SimpleNamespace(store=store, settings=settings))


def test_session_commands_route_and_switch(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    gw = _gw(store, {"threads": {"enabled": True, "autotitle": "off"}})

    assert "No sessions yet" in gw.handle_command(Inbound(user_id="7", text="/sessions"))
    assert "now active" in gw.handle_command(Inbound(user_id="7", text="/new work"))
    assert threads.active_slug(store, "telegram:7") == "work"

    listing = gw.handle_command(Inbound(user_id="7", text="/sessions"))
    assert "work" in listing and "→" in listing            # active marker on the new one

    # /new a second, then /resume back to the first by slug.
    gw.handle_command(Inbound(user_id="7", text="/new travel"))
    assert threads.active_slug(store, "telegram:7") == "travel"
    assert "Resumed 'work'" in gw.handle_command(Inbound(user_id="7", text="/resume work"))
    assert threads.active_slug(store, "telegram:7") == "work"

    # /rename retitles the active session; /delete removes one.
    gw.handle_command(Inbound(user_id="7", text="/rename Work Stuff"))
    active_id = threads.active_session(store, "telegram:7")
    assert threads.thread_meta(store)[active_id]["title"] == "Work Stuff"
    assert "Deleted 'travel'" in gw.handle_command(Inbound(user_id="7", text="/delete travel"))
    assert "travel" not in {r["slug"] for r in threads.user_sessions(store, "telegram:7")}
    store.close()


def test_session_commands_inert_when_threads_off(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    gw = _gw(store, {})                                     # threads off
    # Falls through to the agent (None) rather than acting.
    assert gw.handle_command(Inbound(user_id="7", text="/sessions")) is None
    assert gw.handle_command(Inbound(user_id="7", text="/new work")) is None
    assert threads.active_slug(store, "telegram:7") is None
    store.close()


# ── registry ──────────────────────────────────────────────────────────────────

def test_registry_discovers_without_crashing(tmp_path):
    config = SimpleNamespace(root=tmp_path, settings={})
    names = registry.gateway_names(config)            # no user gateways yet
    assert isinstance(names, list)


def test_registry_unknown_gateway_raises(tmp_path):
    config = SimpleNamespace(root=tmp_path, settings={})
    with pytest.raises(KeyError):
        registry.get_gateway(config, "does-not-exist", _deps(None))


# ── manager: process lifecycle (22e) ──────────────────────────────────────────

def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_pid_alive_self_and_dead():
    assert manager._pid_alive(os.getpid()) is True
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert manager._pid_alive(proc.pid) is False


def test_spawn_argv_targets_the_module(tmp_path):
    config = SimpleNamespace(workspace=tmp_path, root=tmp_path)
    argv = manager._spawn_argv(config, "telegram")
    assert argv[:3] == [sys.executable, "-m", "agent"]
    assert "--gateway" in argv and "telegram" in argv


def test_status_reflects_pid_file(tmp_path):
    config = SimpleNamespace(workspace=tmp_path, root=tmp_path)
    assert manager.status(config, "telegram")["running"] is False
    manager._write_pid(config, "telegram", os.getpid())
    st = manager.status(config, "telegram")
    assert st["running"] is True and st["pid"] == os.getpid()


def test_stale_pid_file_is_cleaned(tmp_path):
    config = SimpleNamespace(workspace=tmp_path, root=tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    manager._write_pid(config, "telegram", proc.pid)
    assert manager.status(config, "telegram")["running"] is False
    assert not manager.pid_path(config, "telegram").exists()


def test_start_idempotent_then_stop(tmp_path, monkeypatch):
    config = SimpleNamespace(workspace=tmp_path, root=tmp_path)
    monkeypatch.setattr(
        manager, "_spawn_argv",
        lambda c, n: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    started = manager.start(config, "telegram")
    try:
        assert started["running"] is True
        assert manager.status(config, "telegram")["running"] is True
        again = manager.start(config, "telegram")          # idempotent
        assert again["pid"] == started["pid"]
    finally:
        assert manager.stop(config, "telegram") is True
    assert _wait_until(lambda: not manager.status(config, "telegram")["running"])
