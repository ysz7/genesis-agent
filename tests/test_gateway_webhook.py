"""Phase 22g: gateway webhooks mounted on ``--serve``.

End-to-end through the real HTTP server: a POST to ``/webhook/telegram`` drives
the same agent the server hosts. The model is a FunctionModel and the outbound
Telegram call is monkeypatched, so nothing leaves the process.
"""

import socket
import time

import httpx
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart

from agent.engine import factory
from agent.gateways.telegram import TelegramGateway
from agent.runtime.config import load_config
from agent.server import start_background, stop_background


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pong_model() -> FunctionModel:
    def fn(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="pong")])
    return FunctionModel(fn)


def _serve_with_telegram(monkeypatch, tmp_path, *, secret=None):
    (tmp_path / "settings.yaml").write_text(
        "store: agent.sqlite\n"
        "gateways:\n  telegram:\n    enabled: true\n    allowlist: [555]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    if secret:
        monkeypatch.setenv("WEBHOOK_SECRET", secret)
    monkeypatch.setattr(factory, "build_model", lambda config: _pong_model())

    sent: list = []

    async def fake_send(self, client, chat_id, text):
        sent.append((chat_id, text))

    async def fake_action(self, client, chat_id, action):
        pass

    monkeypatch.setattr(TelegramGateway, "send_message", fake_send)
    monkeypatch.setattr(TelegramGateway, "send_action", fake_action)

    config = load_config(tmp_path)
    port = _free_port()
    httpd, deps = start_background(config, port=port)
    return httpd, deps, f"http://127.0.0.1:{port}", sent


def _update(text, uid=555):
    return {"update_id": 1, "message": {"chat": {"id": uid}, "from": {"id": uid, "first_name": "T"}, "text": text}}


def _wait(sent, n=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(sent) >= n:
            return True
        time.sleep(0.05)
    return len(sent) >= n


def test_webhook_runs_agent_and_replies(monkeypatch, tmp_path):
    httpd, deps, base, sent = _serve_with_telegram(monkeypatch, tmp_path)
    try:
        r = httpx.post(f"{base}/webhook/telegram", json=_update("ping"), timeout=10)
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert _wait(sent)
        assert sent[0] == (555, "pong")
    finally:
        stop_background(httpd, deps)


def test_webhook_unknown_gateway_404(monkeypatch, tmp_path):
    httpd, deps, base, sent = _serve_with_telegram(monkeypatch, tmp_path)
    try:
        r = httpx.post(f"{base}/webhook/whatsapp", json=_update("ping"), timeout=10)
        assert r.status_code == 404
    finally:
        stop_background(httpd, deps)


def test_webhook_requires_secret_when_set(monkeypatch, tmp_path):
    httpd, deps, base, sent = _serve_with_telegram(monkeypatch, tmp_path, secret="s3cr3t")
    try:
        bad = httpx.post(f"{base}/webhook/telegram", json=_update("ping"), timeout=10)
        assert bad.status_code == 401                       # missing/wrong secret header
        ok = httpx.post(
            f"{base}/webhook/telegram", json=_update("ping"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"}, timeout=10,
        )
        assert ok.status_code == 200
        assert _wait(sent)
    finally:
        stop_background(httpd, deps)
