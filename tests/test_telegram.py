"""Phase 22b: Telegram gateway — pure helpers + handle_update over a mock API.

No network: a ``httpx.MockTransport`` stands in for api.telegram.org and records
every call, so access gating, the agent run, and reply chunking are all observable.
"""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx

from agent.gateways.base import Pipeline
from agent.gateways.telegram import (
    ApprovalBridge, TelegramGateway, chunk_text, normalize_update,
    strip_markdown, telegram_media, to_telegram_html,
)
from agent.runtime.store import open_store


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_chunk_text_short_is_single():
    assert chunk_text("hello") == ["hello"]
    assert chunk_text("") == ["(no output)"]


def test_chunk_text_splits_on_newline_under_limit():
    text = "\n".join(["line"] * 2000)            # well over 4096 chars
    chunks = chunk_text(text, limit=4096)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(c for c in chunks).replace("\n", "") == "line" * 2000


def test_normalize_update_reads_text_and_caption():
    chat_id, inb = normalize_update(
        {"update_id": 1, "message": {"chat": {"id": 7}, "from": {"id": 9, "first_name": "Al"}, "text": "hi"}}
    )
    assert chat_id == 7 and inb.user_id == "9" and inb.text == "hi" and inb.user_name == "Al"
    _, inb2 = normalize_update(
        {"update_id": 2, "message": {"chat": {"id": 7}, "from": {"id": 9}, "caption": "pic!"}}
    )
    assert inb2.text == "pic!"


def test_normalize_update_ignores_non_messages():
    assert normalize_update({"update_id": 3}) is None
    assert normalize_update({"update_id": 4, "message": {"from": {"id": 1}}}) is None


# ── markdown rendering ────────────────────────────────────────────────────────

def test_to_telegram_html_formats_and_escapes():
    h = to_telegram_html("**bold** and `code`\n- item\na < b & c")
    assert "<b>bold</b>" in h
    assert "<code>code</code>" in h
    assert "• item" in h
    assert "a &lt; b &amp; c" in h          # raw <, & are escaped
    assert "**" not in h                      # no leftover markdown


def test_to_telegram_html_code_block():
    h = to_telegram_html("```python\nprint('hi')\n```")
    assert "<pre>" in h and "print('hi')" in h


def test_strip_markdown_flattens():
    assert strip_markdown("**bold**\n- item") == "bold\n• item"
    assert strip_markdown("`x`") == "x"


def test_send_renders_html_by_default(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw.send_message(c, 555, "**hi** there")
    asyncio.run(go())

    body = _sends(records)[0]
    assert body["parse_mode"] == "HTML"
    assert "<b>hi</b>" in body["text"]
    store.close()


def test_send_falls_back_to_plain_on_400(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        sent.append(payload)
        # reject the first (HTML) attempt, accept the plain retry
        if payload.get("parse_mode") == "HTML":
            return httpx.Response(400, json={"ok": False, "description": "bad entities"})
        return httpx.Response(200, json={"ok": True})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await gw.send_message(c, 555, "**hi**")
    asyncio.run(go())

    assert len(sent) == 2                       # HTML attempt + plain fallback
    assert "parse_mode" not in sent[1] and sent[1]["text"] == "hi"
    store.close()


# ── fakes ─────────────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, output):
        self.output = output
    def all_messages(self):
        return []


class _FakeAgent:
    def __init__(self):
        self.histories = []
        self.prompts = []
    async def run(self, prompt, deps=None, message_history=None, usage_limits=None):
        self.histories.append(message_history)
        self.prompts.append(prompt)
        return _Result(f"echo:{prompt}" if isinstance(prompt, str) else "echo:(media)")


def _make_gw(tmp_path, monkeypatch, allowlist=None, owner_id=None):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    store = open_store(tmp_path / "agent.sqlite")
    tg = {"allowlist": allowlist or []}        # parse_mode defaults to html
    if owner_id is not None:
        tg["owner_id"] = owner_id
    settings = {"gateways": {"telegram": tg}, "scheduler": {"enabled": True}}
    config = SimpleNamespace(root=tmp_path, settings=settings, usage_limits=None)
    deps = SimpleNamespace(store=store, extra={})
    gw = TelegramGateway(config, deps)
    gw._pipeline = Pipeline("telegram", _FakeAgent(), deps, settings)
    return gw, store


def _mock_client(records):
    def handler(request: httpx.Request) -> httpx.Response:
        records.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": []})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _update(uid, text, from_id):
    return {"update_id": uid, "message": {"chat": {"id": from_id}, "from": {"id": from_id, "first_name": "T"}, "text": text}}


def _sends(records):
    return [body for path, body in records if path.endswith("/sendMessage")]


# ── handle_update ─────────────────────────────────────────────────────────────

def test_denied_user_gets_refusal_and_no_run(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[])     # deny-all
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw.handle_update(c, _update(1, "hi", 555))
    asyncio.run(go())

    sends = _sends(records)
    assert len(sends) == 1
    assert "555" in sends[0]["text"] and "Access is closed" in sends[0]["text"]
    assert gw._pipeline.agent.histories == []                     # agent never ran
    store.close()


def test_allowed_user_runs_and_replies(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw.handle_update(c, _update(1, "hello", 555))
    asyncio.run(go())

    assert any(p.endswith("/sendChatAction") for p, _ in records)  # typing shown
    sends = _sends(records)
    assert sends[0]["text"] == "echo:hello"
    assert gw._pipeline.agent.histories == [None]                  # ran exactly once
    store.close()


def test_long_reply_is_chunked(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw.handle_update(c, _update(1, "x" * 5000, 555))  # echo: ~5005 chars
    asyncio.run(go())

    sends = _sends(records)
    assert len(sends) == 2                                          # split across the 4096 cap
    assert all(len(s["text"]) <= 4096 for s in sends)
    store.close()


# ── access management commands (22c) ──────────────────────────────────────────

def _run(gw, records, update):
    async def go():
        async with _mock_client(records) as c:
            await gw.handle_update(c, update)
    asyncio.run(go())


def test_owner_bootstraps_without_allowlist(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[], owner_id=111)
    records: list = []
    _run(gw, records, _update(1, "hello", 111))                    # owner, empty allowlist
    assert _sends(records)[0]["text"] == "echo:hello"              # owner always allowed
    store.close()


def test_owner_allow_then_user_can_chat(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[], owner_id=111)
    records: list = []
    _run(gw, records, _update(1, "/allow 222", 111))
    assert "Allowed 222" in _sends(records)[-1]["text"]
    assert gw._pipeline.agent.histories == []                      # a command never runs the agent

    records.clear()
    _run(gw, records, _update(2, "hi there", 222))                 # now 222 is allowed
    assert _sends(records)[0]["text"] == "echo:hi there"
    store.close()


def test_non_owner_cannot_manage(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[222], owner_id=111)
    records: list = []
    _run(gw, records, _update(1, "/allow 333", 222))               # 222 allowed but not owner
    assert _sends(records)[-1]["text"] == "Owner only."
    store.close()


def test_whoami_and_deny(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[], owner_id=111)
    records: list = []
    _run(gw, records, _update(1, "/whoami", 111))
    assert "111" in _sends(records)[-1]["text"]

    gw.access.allow("222")
    records.clear()
    _run(gw, records, _update(2, "/deny 222", 111))
    assert "Removed 222" in _sends(records)[-1]["text"]
    assert gw.access.allowed("222") is False
    store.close()


# ── inbound media (22d) ───────────────────────────────────────────────────────

def test_telegram_media_extraction():
    msg = {"photo": [{"file_id": "s", "width": 90}, {"file_id": "L", "width": 800, "file_size": 9}]}
    assert telegram_media(msg) == [("L", "photo.jpg")]
    assert telegram_media({"document": {"file_id": "D", "file_name": "report.pdf"}}) == [("D", "report.pdf")]
    assert telegram_media({"text": "no media"}) == []


def _media_client(records):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/file_1.jpg"}})
        if "/file/bot" in path:                                   # the binary download
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0JFIF-pretend-jpeg")
        records.append((path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": []})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_photo_is_downloaded_and_attached(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    records: list = []
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 555}, "from": {"id": 555, "first_name": "T"},
            "caption": "what is this?",
            "photo": [{"file_id": "AAA", "width": 800, "file_size": 20}],
        },
    }

    async def go():
        async with _media_client(records) as c:
            await gw.handle_update(c, update)
    asyncio.run(go())

    # the agent saw a multimodal prompt (list with a BinaryContent part)
    from pydantic_ai import BinaryContent

    prompt = gw._pipeline.agent.prompts[0]
    assert isinstance(prompt, list)
    assert any(isinstance(p, BinaryContent) for p in prompt)
    assert _sends(records)                                          # a reply was sent
    store.close()


# ── inline-button approvals (22h) ─────────────────────────────────────────────

def test_approval_bridge_resolves_button_press(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    bridge = ApprovalBridge(gw)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    records: list = []
    client = _mock_client(records)
    bridge.bind(client, loop)

    result: dict = {}

    def call_hook():                                # runs like a sync tool thread
        result["decision"] = bridge.hook_for(555)("run_shell", "rm -rf important/")

    worker = threading.Thread(target=call_hook)
    worker.start()
    # wait for the prompt to register, grab its request id, simulate "Allow once"
    assert _wait_pending(bridge)
    rid = next(iter(bridge._pending))
    cb = {"id": "cb1", "data": f"ap:once:{rid}"}
    asyncio.run_coroutine_threadsafe(bridge.handle_callback(client, cb), loop).result(timeout=3)
    worker.join(timeout=3)

    assert result["decision"] == "once"
    assert any(p.endswith("/sendMessage") for p, _ in records)      # buttons were sent
    loop.call_soon_threadsafe(loop.stop)
    store.close()


def test_approval_callback_routed_through_handle_update(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    seen: dict = {}

    class _Spy:
        async def handle_callback(self, client, cb):
            seen["data"] = cb.get("data")
    gw._bridge = _Spy()

    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw.handle_update(c, {"update_id": 1, "callback_query": {"id": "x", "data": "ap:deny:zz"}})
    asyncio.run(go())

    assert seen["data"] == "ap:deny:zz"                             # callback reached the bridge
    store.close()


def _wait_pending(bridge, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bridge._pending:
            return True
        time.sleep(0.02)
    return bool(bridge._pending)


# ── monitor hooks ─────────────────────────────────────────────────────────────

class _Mon:
    def __init__(self):
        self.messages, self.replies, self.blocked = [], [], []
    def on_message(self, uid, name, text):
        self.messages.append((uid, text))
    def on_reply(self, ok, tokens, elapsed, preview=""):
        self.replies.append((ok, tokens, preview))
    def on_blocked(self, uid):
        self.blocked.append(uid)


def test_monitor_receives_message_and_reply(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    gw.monitor = _Mon()
    records: list = []
    _run(gw, records, _update(1, "hi", 555))
    assert gw.monitor.messages == [("555", "hi")]
    assert gw.monitor.replies and gw.monitor.replies[0][0] is True
    assert gw.monitor.replies[0][2] == "echo:hi"
    store.close()


def test_monitor_records_blocked(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[])      # deny-all
    gw.monitor = _Mon()
    records: list = []
    _run(gw, records, _update(1, "hi", 999))
    assert gw.monitor.blocked == ["999"]
    assert gw.monitor.messages == []
    store.close()


# ── scheduler tick + delivery (23e) ───────────────────────────────────────────

def test_scheduler_tick_runs_and_delivers(tmp_path, monkeypatch):
    from agent.runtime import scheduler

    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555], owner_id=111)
    job = scheduler.add_job(store, "ping", 60)
    jobs = scheduler.list_jobs(store)
    jobs[0]["next_run"] = 1                                # force due
    store.set(scheduler.KEY, jobs)

    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw._tick_scheduler(c)
    asyncio.run(go())

    assert scheduler.get_job(store, job["id"])["runs"] == 1       # job fired + bumped
    sends = _sends(records)
    assert {s["chat_id"] for s in sends} == {"555", "111"}        # allowlist + owner
    assert all(s["text"].startswith("⏰") and "echo:ping" in s["text"] for s in sends)
    store.close()


def test_scheduler_tick_off_when_disabled(tmp_path, monkeypatch):
    from agent.runtime import scheduler

    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    gw._scheduler_on = False
    scheduler.add_job(store, "ping", 60)
    jobs = scheduler.list_jobs(store)
    jobs[0]["next_run"] = 1
    store.set(scheduler.KEY, jobs)
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw._tick_scheduler(c)
    asyncio.run(go())

    assert _sends(records) == [] and scheduler.list_jobs(store)[0]["runs"] == 0
    store.close()


# ── quota + rate-limit backoff (22i) ──────────────────────────────────────────

def test_quota_blocks_after_cap(tmp_path, monkeypatch):
    from agent.gateways.base import Quota

    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    gw._quota = Quota(store, "telegram", 1)              # one message/day
    records: list = []
    _run(gw, records, _update(1, "hi", 555))
    _run(gw, records, _update(2, "again", 555))
    texts = [b["text"] for b in _sends(records)]
    assert texts[0] == "echo:hi"
    assert "today's message limit" in texts[-1]
    assert len(gw._pipeline.agent.histories) == 1       # agent ran only once
    store.close()


def test_send_message_retries_once_on_429(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[555])
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
        return httpx.Response(200, json={"ok": True})

    async def _noop(*a, **k):
        pass
    monkeypatch.setattr(asyncio, "sleep", _noop)        # don't actually wait

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await gw.send_message(c, 555, "hi")
    asyncio.run(go())

    assert calls["n"] == 2                               # initial + one retry
    store.close()
