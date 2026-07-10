"""Phase 18: persistent conversation threads.

Unit round-trips over the real store (JSON file = a simulated restart), plus
end-to-end through the HTTP server's optional ``session`` (a FunctionModel that
reports how many messages it was handed, so loaded history is observable).
"""

import socket

import httpx
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import (
    ModelRequest, ModelResponse, UserPromptPart, TextPart,
)

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
