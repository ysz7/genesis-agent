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
    deps = SimpleNamespace(store=store)
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
