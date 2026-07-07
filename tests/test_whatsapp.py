"""Phase 22j: WhatsApp gateway — payload parsing, verify handshake, send.

No network: a ``httpx.MockTransport`` stands in for the Meta Graph API.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx

from agent.gateways.base import Pipeline
from agent.gateways.whatsapp import WhatsAppGateway, whatsapp_messages
from agent.runtime.store import open_store


class _Result:
    def __init__(self, output):
        self.output = output
    def all_messages(self):
        return []


class _FakeAgent:
    def __init__(self):
        self.prompts = []
    async def run(self, prompt, deps=None, message_history=None, usage_limits=None):
        self.prompts.append(prompt)
        return _Result(f"echo:{prompt}" if isinstance(prompt, str) else "echo:(media)")


def _make_gw(tmp_path, monkeypatch, allowlist=None):
    monkeypatch.setenv("WHATSAPP_TOKEN", "T")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "PHONE1")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verifyme")
    store = open_store(tmp_path / "agent.sqlite")
    settings = {"gateways": {"whatsapp": {"allowlist": allowlist or []}}}
    config = SimpleNamespace(root=tmp_path, settings=settings, usage_limits=None)
    deps = SimpleNamespace(store=store, extra={}, settings={})
    gw = WhatsAppGateway(config, deps)
    gw._pipeline = Pipeline("whatsapp", _FakeAgent(), deps, settings)
    return gw, store


def _mock_client(records):
    def handler(request: httpx.Request) -> httpx.Response:
        records.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"messaging_product": "whatsapp"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _inbound(text, frm="15551230000"):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": frm, "type": "text", "text": {"body": text}}
    ]}}]}]}


# ── parsing ───────────────────────────────────────────────────────────────────

def test_whatsapp_messages_parses_text():
    pairs = whatsapp_messages(_inbound("hello", "999"))
    assert len(pairs) == 1
    inbound, to = pairs[0]
    assert inbound.user_id == "999" and inbound.text == "hello" and to == "999"


def test_whatsapp_messages_ignores_status_callbacks():
    assert whatsapp_messages({"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]}) == []


# ── verify handshake ──────────────────────────────────────────────────────────

def test_webhook_verify_echoes_challenge(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch)
    ok = gw.webhook_verify({"hub.mode": ["subscribe"], "hub.verify_token": ["verifyme"], "hub.challenge": ["12345"]})
    assert ok == (200, "12345")
    bad = gw.webhook_verify({"hub.mode": ["subscribe"], "hub.verify_token": ["wrong"], "hub.challenge": ["12345"]})
    assert bad[0] == 403
    store.close()


# ── handle_update ─────────────────────────────────────────────────────────────

def test_denied_number_gets_refusal(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[])      # deny-all
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw.handle_update(c, _inbound("hi", "999"))
    asyncio.run(go())

    sent = [b for p, b in records if p.endswith("/messages")]
    assert "Access is closed" in sent[0]["text"]["body"]
    assert gw._pipeline.agent.prompts == []                       # agent never ran
    store.close()


def test_allowed_number_runs_and_replies(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=["999"])
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw.handle_update(c, _inbound("hi", "999"))
    asyncio.run(go())

    sent = [b for p, b in records if p.endswith("/messages")]
    assert sent[0]["to"] == "999"
    assert sent[0]["text"]["body"] == "echo:hi"
    store.close()


def test_run_refuses_polling(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch)
    try:
        asyncio.run(gw.run())
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "webhook-only" in str(exc)
    store.close()


# ── formatting (25f) ──────────────────────────────────────────────────────────

def test_to_whatsapp_formatting():
    from agent.gateways.whatsapp import to_whatsapp

    out = to_whatsapp("**bold** and *ital* and `code`\n# Head\n[t](https://x.y)")
    assert "*bold*" in out                       # ** → *
    assert "_ital_" in out                       # * → _
    assert "```code```" in out                   # inline code → monospace
    assert "*Head*" in out                       # heading → bold
    assert "t (https://x.y)" in out              # link flattened
    assert "**" not in out and "#" not in out


def test_outbound_uses_whatsapp_formatting(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=["999"])
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw.send_message(c, "999", "**hi** there")
    asyncio.run(go())

    body = [b for p, b in records if p.endswith("/messages")][0]["text"]["body"]
    assert body == "*hi* there"
    store.close()


# ── management commands (25b) ─────────────────────────────────────────────────

def test_owner_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_ID", "111")
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=[])
    records: list = []

    async def go(update):
        async with _mock_client(records) as c:
            await gw.handle_update(c, update)

    asyncio.run(go(_inbound("/allow 222", "111")))              # owner bootstraps
    sent = [b for p, b in records if p.endswith("/messages")]
    assert "Allowed 222" in sent[-1]["text"]["body"]

    records.clear()
    asyncio.run(go(_inbound("hello", "222")))                    # 222 now allowed
    sent = [b for p, b in records if p.endswith("/messages")]
    assert sent[-1]["text"]["body"] == "echo:hello"

    records.clear()
    asyncio.run(go(_inbound("/whoami", "222")))
    sent = [b for p, b in records if p.endswith("/messages")]
    assert "222" in sent[-1]["text"]["body"] and "number" in sent[-1]["text"]["body"]

    records.clear()
    asyncio.run(go(_inbound("/allow 333", "222")))               # non-owner blocked
    sent = [b for p, b in records if p.endswith("/messages")]
    assert sent[-1]["text"]["body"] == "Owner only."
    store.close()


# ── inbound media (25c) ───────────────────────────────────────────────────────

def _media_inbound(frm="999"):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": frm, "id": "wamid.1", "type": "image",
         "image": {"id": "MEDIA1", "mime_type": "image/jpeg", "caption": "what is this?"}}
    ]}}]}]}


def test_whatsapp_media_extraction():
    from agent.gateways.whatsapp import whatsapp_media

    msg = {"type": "image", "image": {"id": "M1", "mime_type": "image/jpeg"}}
    assert whatsapp_media(msg) == [("M1", "image.jpg")]
    doc = {"type": "document", "document": {"id": "D1", "filename": "r.pdf"}}
    assert whatsapp_media(doc) == [("D1", "r.pdf")]
    assert whatsapp_media({"type": "text"}) == []


def test_photo_downloaded_and_attached(tmp_path, monkeypatch):
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=["999"])
    records: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/MEDIA1"):                              # media metadata
            return httpx.Response(200, json={"url": "https://cdn.meta/x.jpg"})
        if "cdn.meta" in str(request.url):                        # the binary
            return httpx.Response(200, content=b"\xff\xd8\xffJFIF-bytes")
        records.append((path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await gw.handle_update(c, _media_inbound())
    asyncio.run(go())

    from pydantic_ai import BinaryContent

    prompt = gw._pipeline.agent.prompts[0]
    assert isinstance(prompt, list)                               # multimodal prompt
    assert any(isinstance(p, BinaryContent) for p in prompt)
    store.close()


# ── approval buttons (25d) ────────────────────────────────────────────────────

def test_button_reply_resolves_bridge(tmp_path, monkeypatch):
    from agent.gateways.whatsapp import WhatsAppApprovalBridge

    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=["999"])
    gw._bridge = WhatsAppApprovalBridge(gw)
    seen = {}
    gw._bridge.resolve = lambda payload: seen.setdefault("p", payload)  # spy

    update = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "999", "type": "interactive",
         "interactive": {"type": "button_reply", "button_reply": {"id": "ap:once:abc", "title": "Allow"}}}
    ]}}]}]}

    async def go():
        async with _mock_client([]) as c:
            await gw.handle_update(c, update)
    asyncio.run(go())

    assert seen["p"] == "ap:once:abc"
    assert gw._pipeline.agent.prompts == []                       # never hit the agent
    store.close()


def test_send_buttons_payload(tmp_path, monkeypatch):
    from agent.gateways.whatsapp import WhatsAppApprovalBridge

    gw, store = _make_gw(tmp_path, monkeypatch)
    bridge = WhatsAppApprovalBridge(gw)
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await bridge.send_buttons(c, "999", "run_shell", "rm -rf /x", "abc")
    asyncio.run(go())

    payload = [b for p, b in records if p.endswith("/messages")][0]
    btns = payload["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in btns] == ["ap:once:abc", "ap:always:abc", "ap:deny:abc"]
    store.close()


# ── Meta signature (25e) ──────────────────────────────────────────────────────

def test_signature_verification(tmp_path, monkeypatch):
    import hashlib as _hashlib
    import hmac as _hmac

    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    gw, store = _make_gw(tmp_path, monkeypatch)
    raw = b'{"entry": []}'
    good = "sha256=" + _hmac.new(b"app-secret", raw, _hashlib.sha256).hexdigest()
    assert gw.webhook_authorized({"x-hub-signature-256": good}, raw, None) is True
    assert gw.webhook_authorized({"x-hub-signature-256": "sha256=bad"}, raw, None) is False
    assert gw.webhook_authorized({}, raw, None) is False
    store.close()


def test_signature_open_without_app_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    gw, store = _make_gw(tmp_path, monkeypatch)
    assert gw.webhook_authorized({}, b"{}", "generic") is True    # Meta can't send it
    store.close()


# ── scheduled delivery (25a) ──────────────────────────────────────────────────

def test_deliver_pending_sends_to_allowlist(tmp_path, monkeypatch):
    from agent.runtime import scheduler

    monkeypatch.setenv("WHATSAPP_OWNER_ID", "111")
    gw, store = _make_gw(tmp_path, monkeypatch, allowlist=["999"])
    job = scheduler.add_job(store, "ping", 60)
    scheduler.enqueue_delivery(store, job, "the result")
    records: list = []

    async def go():
        async with _mock_client(records) as c:
            await gw.deliver_pending(c)
    asyncio.run(go())

    sent = [b for p, b in records if p.endswith("/messages")]
    assert {s["to"] for s in sent} == {"999", "111"}              # allowlist + owner
    assert all("the result" in s["text"]["body"] for s in sent)
    assert scheduler.pending_for(store, "whatsapp") == []         # marked consumed
    store.close()
