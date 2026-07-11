"""Phase 18: persistent conversation threads.

Unit round-trips over the real store (JSON file = a simulated restart), plus
end-to-end through the HTTP server's optional ``session`` (a FunctionModel that
reports how many messages it was handed, so loaded history is observable).
"""

import asyncio
import socket

import httpx
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import (
    ModelRequest, ModelResponse, UserPromptPart, TextPart,
)
from pydantic_ai.usage import RunUsage

from agent.engine import factory
from agent.runtime import threads
from agent.runtime.config import load_config
from agent.runtime.store import open_store
from agent.server import start_background, stop_background


def _msgs(secret="BLUE-42"):
    return [
        ModelRequest(parts=[UserPromptPart(content=f"my secret is {secret}")]),
        ModelResponse(parts=[TextPart(content="noted")]),
    ]


# ── Unit: store round-trip ───────────────────────────────────────────────────

def test_thread_roundtrip_across_restart(tmp_path):
    path = tmp_path / "state.json"
    store = open_store(path)
    threads.save_thread(store, "work", _msgs("BLUE-42"))
    store.close()
    # "restart": a brand-new store over the same persisted file
    store2 = open_store(path)
    loaded = threads.load_thread(store2, "work")
    assert len(loaded) == 2
    assert "BLUE-42" in str(loaded)
    store2.close()


def test_thread_cap_respected(tmp_path):
    store = open_store(tmp_path / "state.json")
    long = [ModelRequest(parts=[UserPromptPart(content=f"m{i}")]) for i in range(10)]
    threads.save_thread(store, "s", long, keep=4)
    loaded = threads.load_thread(store, "s")
    assert len(loaded) == 4
    assert "m9" in str(loaded) and "m0" not in str(loaded)
    store.close()


def test_corrupt_thread_degrades_to_fresh(tmp_path):
    store = open_store(tmp_path / "state.json")
    store.set("thread:bad", {"not": "valid messages"})
    assert threads.load_thread(store, "bad") == []      # no crash
    assert threads.load_thread(store, "missing") == []  # absent → fresh
    store.close()


def test_list_and_clear(tmp_path):
    store = open_store(tmp_path / "state.json")
    threads.save_thread(store, "a", _msgs())
    threads.save_thread(store, "b", _msgs())
    assert set(threads.list_threads(store)) == {"a", "b"}
    threads.clear_thread(store, "a")
    assert threads.list_threads(store) == ["b"]
    assert threads.load_thread(store, "a") == []
    store.close()


# ── Phase 36: thread metadata + legacy migration ─────────────────────────────

def test_save_thread_writes_and_updates_meta(tmp_path):
    store = open_store(tmp_path / "state.json")
    threads.save_thread(store, "work", _msgs(), channel="cli")
    meta = threads.thread_meta(store)["work"]
    assert meta["channel"] == "cli"
    assert meta["msg_count"] == 2
    assert meta["title"] == ""                    # title is filled by Phase 37
    first_stamp = meta["updated_at"]
    assert first_stamp is not None

    # A later save advances updated_at and tracks the (capped) message count.
    long = [ModelRequest(parts=[UserPromptPart(content=f"m{i}")]) for i in range(6)]
    threads.save_thread(store, "work", long, keep=4, channel="cli")
    meta2 = threads.thread_meta(store)["work"]
    assert meta2["msg_count"] == 4                 # keep-trimmed count is recorded
    assert meta2["updated_at"] >= first_stamp
    store.close()


def test_meta_records_channel_per_writer(tmp_path):
    store = open_store(tmp_path / "state.json")
    threads.save_thread(store, "telegram:42", _msgs(), channel="telegram")
    threads.save_thread(store, "s", _msgs())      # no channel given
    meta = threads.thread_meta(store)
    assert meta["telegram:42"]["channel"] == "telegram"
    assert meta["s"]["channel"] == ""             # absent channel → empty, not a crash
    store.close()


def test_legacy_flat_index_migrates_on_first_read(tmp_path):
    store = open_store(tmp_path / "state.json")
    # Simulate a pre-Phase-36 store: a flat index with no meta map.
    store.set("threads:index", ["old-a", "old-b"])
    meta = threads.thread_meta(store)             # first read migrates
    assert set(meta) == {"old-a", "old-b"}
    assert meta["old-a"] == {
        "title": "", "updated_at": None, "channel": "", "msg_count": 0
    }
    # Persisted + idempotent: the raw store now holds meta, second read is stable.
    assert set(store.get("threads:meta")) == {"old-a", "old-b"}
    assert threads.thread_meta(store) == meta
    store.close()


def test_clear_thread_removes_meta(tmp_path):
    store = open_store(tmp_path / "state.json")
    threads.save_thread(store, "a", _msgs(), channel="cli")
    threads.save_thread(store, "b", _msgs(), channel="cli")
    threads.clear_thread(store, "a")
    assert "a" not in threads.thread_meta(store)
    assert "b" in threads.thread_meta(store)
    store.close()


# ── Phase 37: auto-titled threads ────────────────────────────────────────────

def _title_model(calls: list, title: str = "Budget Trip to Japan") -> FunctionModel:
    """A FunctionModel that records each call and returns a fixed title."""
    def fn(messages, info: AgentInfo) -> ModelResponse:
        calls.append(1)
        return ModelResponse(parts=[TextPart(content=title)])
    return FunctionModel(fn)


def test_autotitle_cheap_generates_once_and_persists(tmp_path):
    store = open_store(tmp_path / "state.json")
    calls: list = []
    model = _title_model(calls)
    settings = {"threads": {"enabled": True, "autotitle": "cheap"}}

    threads.save_thread(store, "s", _msgs(), channel="cli")
    title = asyncio.run(
        threads.autotitle_thread(store, "s", _msgs(), settings, model=model)
    )
    assert title == "Budget Trip to Japan"
    assert threads.thread_meta(store)["s"]["title"] == "Budget Trip to Japan"
    assert len(calls) == 1                         # one small call for the title

    # A second turn is already titled → no further model call, title unchanged.
    title2 = asyncio.run(
        threads.autotitle_thread(store, "s", _msgs(), settings, model=model)
    )
    assert title2 is None
    assert len(calls) == 1                         # stored, not regenerated
    assert threads.thread_meta(store)["s"]["title"] == "Budget Trip to Japan"
    store.close()


def test_autotitle_off_uses_first_message_and_makes_zero_model_calls(tmp_path):
    store = open_store(tmp_path / "state.json")
    calls: list = []
    model = _title_model(calls, title="SHOULD NOT BE USED")
    settings = {"threads": {"enabled": True, "autotitle": "off"}}

    threads.save_thread(store, "s", _msgs("ZZZ"), channel="cli")
    title = asyncio.run(
        threads.autotitle_thread(store, "s", _msgs("ZZZ"), settings, model=model)
    )
    assert title == "my secret is ZZZ"            # trimmed first user message
    assert calls == []                             # free fallback: no model call
    assert threads.thread_meta(store)["s"]["title"] == "my secret is ZZZ"
    store.close()


def test_autotitle_usage_folds_into_run(tmp_path):
    store = open_store(tmp_path / "state.json")
    model = _title_model([], title="A Concise Title")
    settings = {"threads": {"enabled": True, "autotitle": "cheap"}}
    usage = RunUsage()

    threads.save_thread(store, "s", _msgs(), channel="cli")
    asyncio.run(
        threads.autotitle_thread(store, "s", _msgs(), settings, model=model, usage=usage)
    )
    # The bounded title call bills real tokens into the run's accumulator.
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    store.close()


def test_autotitle_cheap_degrades_without_a_model(tmp_path):
    store = open_store(tmp_path / "state.json")
    settings = {"threads": {"enabled": True, "autotitle": "cheap"}}
    threads.save_thread(store, "s", _msgs("ABC"), channel="cli")
    # No model available → cheap tier degrades to the trimmed-first-message fallback.
    title = asyncio.run(
        threads.autotitle_thread(store, "s", _msgs("ABC"), settings, model=None)
    )
    assert title == "my secret is ABC"
    store.close()


def test_autotitle_noop_when_threads_off(tmp_path):
    store = open_store(tmp_path / "state.json")
    calls: list = []
    model = _title_model(calls)
    threads.save_thread(store, "s", _msgs())
    title = asyncio.run(
        threads.autotitle_thread(
            store, "s", _msgs(), {"threads": {"enabled": False}}, model=model
        )
    )
    assert title is None
    assert calls == []
    assert threads.thread_meta(store)["s"]["title"] == ""   # untouched
    store.close()


# ── Phase 38: CLI session browser (the pure functions it renders) ────────────

def _stamp(store, session_id, iso):
    """Force a known updated_at so recency ordering is deterministic in tests."""
    meta = store.get("threads:meta")
    meta[session_id]["updated_at"] = iso
    store.set("threads:meta", meta)


def test_sessions_by_recency_newest_first_across_channels(tmp_path):
    store = open_store(tmp_path / "state.json")
    threads.save_thread(store, "a", _msgs(), channel="cli")
    threads.save_thread(store, "b", _msgs(), channel="telegram")
    threads.save_thread(store, "c", _msgs(), channel="server")
    _stamp(store, "a", "2026-07-01T10:00:00+00:00")
    _stamp(store, "b", "2026-07-03T10:00:00+00:00")
    _stamp(store, "c", "2026-07-02T10:00:00+00:00")

    rows = threads.sessions_by_recency(store)
    assert [r["id"] for r in rows] == ["b", "c", "a"]           # newest first
    assert [r["channel"] for r in rows] == ["telegram", "server", "cli"]  # all channels
    assert threads.most_recent_session(store) == "b"
    store.close()


def test_most_recent_session_none_when_empty(tmp_path):
    store = open_store(tmp_path / "state.json")
    assert threads.most_recent_session(store) is None
    assert threads.sessions_by_recency(store) == []
    store.close()


def test_sessions_by_recency_lists_legacy_threads_last(tmp_path):
    store = open_store(tmp_path / "state.json")
    threads.save_thread(store, "new", _msgs(), channel="cli")
    # A pre-Phase-36 id that lives only in the flat index (no timestamp).
    store.set("threads:index", (store.get("threads:index") or []) + ["legacy"])
    ids = [r["id"] for r in threads.sessions_by_recency(store)]
    assert ids[0] == "new"        # timestamped session first
    assert ids[-1] == "legacy"    # migrated in, but no updated_at → sorts last
    store.close()


def test_resume_target_picks_most_recent_and_falls_back(tmp_path):
    store = open_store(tmp_path / "state.json")
    on = {"threads": {"enabled": True}}
    # No sessions yet → fresh REPL (None).
    assert threads.resume_target(store, on) is None
    threads.save_thread(store, "a", _msgs(), channel="cli")
    threads.save_thread(store, "b", _msgs(), channel="cli")
    _stamp(store, "a", "2026-07-01T10:00:00+00:00")
    _stamp(store, "b", "2026-07-05T10:00:00+00:00")
    assert threads.resume_target(store, on) == "b"                 # resumes newest
    assert threads.resume_target(store, on, "a") == "a"           # explicit id wins
    # Threads off → always a fresh REPL, even with sessions on disk.
    assert threads.resume_target(store, {"threads": {"enabled": False}}) is None
    store.close()


def test_rename_thread_sets_title(tmp_path):
    store = open_store(tmp_path / "state.json")
    threads.save_thread(store, "a", _msgs(), channel="cli")
    threads.rename_thread(store, "a", "  Weekend Trip Plan  ")
    assert threads.thread_meta(store)["a"]["title"] == "Weekend Trip Plan"  # trimmed
    store.close()


def test_delete_drops_session_from_browser_list(tmp_path):
    store = open_store(tmp_path / "state.json")
    threads.save_thread(store, "a", _msgs(), channel="cli")
    threads.save_thread(store, "b", _msgs(), channel="cli")
    threads.clear_thread(store, "a")
    assert [r["id"] for r in threads.sessions_by_recency(store)] == ["b"]
    store.close()


def test_relative_time_formatting():
    from datetime import datetime, timedelta, timezone

    from agent.console import display

    now = datetime.now(timezone.utc)
    assert display.relative_time(None) == "—"
    assert display.relative_time("not-a-date") == "—"
    assert display.relative_time((now - timedelta(seconds=5)).isoformat()) == "just now"
    assert display.relative_time((now - timedelta(minutes=7)).isoformat()) == "7m ago"
    assert display.relative_time((now - timedelta(hours=3)).isoformat()) == "3h ago"
    assert display.relative_time((now - timedelta(days=2)).isoformat()) == "2d ago"
    assert display.relative_time((now - timedelta(days=20)).isoformat()) == "2w ago"


# ── Phase 39: gateway per-user sessions (pure helpers) ───────────────────────

def test_active_session_defaults_to_main_for_first_time_user(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    assert threads.active_slug(store, "telegram:99") is None      # nothing yet
    sid = threads.active_session(store, "telegram:99")
    assert sid == "telegram:99:main"                              # sane default
    assert threads.active_slug(store, "telegram:99") == "main"    # pointer persisted
    store.close()


def test_new_and_resume_switch_active_session(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    prefix = "telegram:7"
    threads.active_session(store, prefix)                          # 'main'
    slug = threads.new_session(store, prefix, "Work Notes", channel="telegram")
    assert slug == "work-notes"                                    # name slugified
    assert threads.active_slug(store, prefix) == "work-notes"      # new one active
    # /sessions lists both, newest (the new one) first, with slugs.
    rows = threads.user_sessions(store, prefix)
    assert {r["slug"] for r in rows} == {"main", "work-notes"}
    assert rows[0]["slug"] == "work-notes"                         # newest first
    # resolve by 1-based index and by slug, then switch back to main.
    assert threads.resolve_session(store, prefix, "1") == "work-notes"
    assert threads.resolve_session(store, prefix, "2") == "main"
    assert threads.resolve_session(store, prefix, "main") == "main"
    assert threads.resolve_session(store, prefix, "nope") is None
    threads.set_active(store, prefix, "main")
    assert threads.active_slug(store, prefix) == "main"
    store.close()


def test_new_session_dedupes_slug(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    prefix = "telegram:7"
    assert threads.new_session(store, prefix, "trip") == "trip"
    assert threads.new_session(store, prefix, "trip") == "trip-2"  # collision → suffix
    store.close()


def test_delete_active_session_repoints_pointer(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    prefix = "telegram:7"
    threads.save_thread(store, "telegram:7:main", _msgs(), channel="telegram")
    threads.new_session(store, prefix, "work", channel="telegram")  # active = work
    threads.save_thread(store, "telegram:7:work", _msgs(), channel="telegram")
    threads.delete_session(store, prefix, "work")                   # delete the active one
    assert "telegram:7:work" not in [r["id"] for r in threads.user_sessions(store, prefix)]
    assert threads.active_slug(store, prefix) == "main"             # repointed to what's left
    store.close()


def test_user_sessions_isolates_users_and_channels(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    threads.save_thread(store, "telegram:12:main", _msgs(), channel="telegram")
    threads.save_thread(store, "telegram:123:main", _msgs(), channel="telegram")  # different user
    threads.save_thread(store, "whatsapp:12:main", _msgs(), channel="whatsapp")   # different channel
    slugs = [r["id"] for r in threads.user_sessions(store, "telegram:12")]
    assert slugs == ["telegram:12:main"]           # trailing ':' stops 12 matching 123
    store.close()


def test_active_session_adopts_legacy_single_thread(tmp_path):
    store = open_store(tmp_path / "agent.sqlite")
    # A pre-Phase-39 gateway thread lives at the bare prefix id.
    threads.save_thread(store, "telegram:7", _msgs("LEGACY-1"), channel="telegram")
    sid = threads.active_session(store, "telegram:7")               # first multi-session use
    assert sid == "telegram:7:main"
    moved = threads.load_thread(store, "telegram:7:main")
    assert "LEGACY-1" in str(moved)                                 # history carried over
    assert threads.load_thread(store, "telegram:7") == []           # old id retired
    assert "telegram:7" not in [r["id"] for r in threads.sessions_by_recency(store)]
    store.close()


# ── End-to-end: server session ───────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _len_model() -> FunctionModel:
    """Reports how many messages it received — so loaded history is observable."""
    def fn(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=f"seen:{len(messages)}")])
    return FunctionModel(fn)


def _serve(monkeypatch, tmp_path, settings_yaml):
    (tmp_path / "settings.yaml").write_text(settings_yaml, encoding="utf-8")
    monkeypatch.setattr(factory, "build_model", lambda config: _len_model())
    config = load_config(tmp_path)
    port = _free_port()
    httpd, deps = start_background(config, port=port)
    return httpd, deps, f"http://127.0.0.1:{port}"


def test_server_session_roundtrips_when_enabled(monkeypatch, tmp_path):
    httpd, deps, base = _serve(monkeypatch, tmp_path, "threads:\n  enabled: true\n")
    try:
        n1 = httpx.post(f"{base}/task", json={"task": "hi", "session": "s"}, timeout=10).json()["output"]
        n2 = httpx.post(f"{base}/task", json={"task": "again", "session": "s"}, timeout=10).json()["output"]
        assert n1 == "seen:1"                       # first call: only the new request
        assert int(n2.split(":")[1]) > 1            # second: prior history loaded
    finally:
        stop_background(httpd, deps)


def test_server_stateless_without_session(monkeypatch, tmp_path):
    httpd, deps, base = _serve(monkeypatch, tmp_path, "threads:\n  enabled: true\n")
    try:
        a = httpx.post(f"{base}/task", json={"task": "hi"}, timeout=10).json()["output"]
        b = httpx.post(f"{base}/task", json={"task": "hi"}, timeout=10).json()["output"]
        assert a == "seen:1" and b == "seen:1"      # no session → no memory
    finally:
        stop_background(httpd, deps)


def test_server_session_ignored_when_threads_off(monkeypatch, tmp_path):
    httpd, deps, base = _serve(monkeypatch, tmp_path, "name: x\n")  # threads off
    try:
        httpx.post(f"{base}/task", json={"task": "hi", "session": "s"}, timeout=10)
        out = httpx.post(f"{base}/task", json={"task": "hi", "session": "s"}, timeout=10).json()["output"]
        assert out == "seen:1"                       # session ignored → stateless
    finally:
        stop_background(httpd, deps)
