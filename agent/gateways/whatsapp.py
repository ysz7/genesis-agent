"""WhatsApp channel — Meta Cloud API, webhook-only (Phase 22j · parity 25).

WhatsApp has no long-poll; Meta delivers messages to a public webhook. So this
channel only runs under ``agent --serve`` (mounted at ``POST /webhook/whatsapp``
plus the ``GET`` verification handshake Meta requires). Pure ``httpx`` against
the Graph API — no SDK.

Functional parity with the Telegram channel (Phase 25):

- shared :class:`~agent.gateways.base.Pipeline` with a per-sender thread
  (``whatsapp:<phone>``), deny-all allowlist + owner bootstrap, and the shared
  management commands (``/allow`` · ``/deny`` · ``/allowlist`` · ``/whoami``);
- inbound **media** (image/document/audio) downloaded via the Graph media
  endpoint and attached (vision parts / inlined text docs);
- tool approvals as **interactive reply buttons** (Allow once / Always / Deny);
- scheduled-task delivery to all allowlisted numbers (drained by the server's
  scheduler ticker — this channel has no loop of its own);
- webhook authentication via Meta's ``X-Hub-Signature-256`` (HMAC-SHA256 of the
  raw body with ``WHATSAPP_APP_SECRET``);
- replies rendered in WhatsApp's own formatting (``*bold*``, ``_italic_``,
  ```` ``` ````-monospace) instead of raw markdown.

Secrets in ``.env``: ``WHATSAPP_TOKEN`` (Graph API token), ``WHATSAPP_PHONE_ID``
(the sender phone-number id), ``WHATSAPP_VERIFY_TOKEN`` (the GET-handshake string
you choose in the Meta dashboard), ``WHATSAPP_APP_SECRET`` (the app secret used
to sign webhook posts — set it; without it inbound posts are unauthenticated),
optional ``WHATSAPP_OWNER_ID`` (your number).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from .base import ApprovalBridgeBase, Gateway, Inbound, store_guard
from .telegram import (
    _RE_BOLD, _RE_CODE_BLOCK, _RE_HEADING, _RE_INLINE_CODE, _RE_ITALIC, _RE_LINK,
    chunk_text,
)

logger = logging.getLogger("agent.gateways.whatsapp")

GRAPH = "https://graph.facebook.com/v19.0"
MSG_LIMIT = 4096            # WhatsApp cap on a text body
MEDIA_TYPES = ("image", "document", "audio", "video")


# ── markdown → WhatsApp formatting (25f) ──────────────────────────────────────
# WhatsApp renders its own lightweight syntax: *bold*, _italic_, ```monospace```.
# Convert the model's CommonMark instead of flattening it.

def to_whatsapp(text: str) -> str:
    """Convert CommonMark-ish markdown to WhatsApp's native formatting."""
    blocks: list[str] = []

    def _stash(payload: str) -> str:
        blocks.append(payload)
        return f"\x00B{len(blocks) - 1}\x00"

    text = _RE_CODE_BLOCK.sub(lambda m: _stash("```" + m.group(1).rstrip("\n") + "```"), text)
    text = _RE_INLINE_CODE.sub(lambda m: _stash("```" + m.group(1) + "```"), text)
    text = _RE_HEADING.sub(lambda m: _stash("*" + m.group(1) + "*"), text)
    text = _RE_BOLD.sub(lambda m: _stash("*" + m.group(1) + "*"), text)      # ** → *
    text = _RE_ITALIC.sub(lambda m: f"_{m.group(1)}_", text)                  # * → _
    text = _RE_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    for i, blk in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", blk)
    return text


# ── inbound parsing ───────────────────────────────────────────────────────────

def whatsapp_messages(update: dict) -> list[tuple[Inbound, str]]:
    """Extract ``(Inbound, to)`` pairs from a Meta webhook payload.

    Meta nests messages under ``entry[].changes[].value.messages[]``; status
    callbacks (delivered/read) carry no ``messages`` and yield nothing. The raw
    message is kept on ``Inbound.raw`` for the media/button paths.
    """
    out: list[tuple[Inbound, str]] = []
    for entry in update.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                (c.get("wa_id") or ""): ((c.get("profile") or {}).get("name") or "")
                for c in value.get("contacts") or []
            }
            for msg in value.get("messages") or []:
                sender = msg.get("from")
                if not sender:
                    continue
                mtype = msg.get("type")
                if mtype == "text":
                    text = (msg.get("text") or {}).get("body", "")
                elif mtype == "interactive":
                    text = ""                    # button replies carry no prose
                else:
                    text = (msg.get(mtype) or {}).get("caption", "") or ""
                out.append((
                    Inbound(user_id=str(sender), text=text,
                            user_name=names.get(str(sender), ""), raw=msg),
                    str(sender),
                ))
    return out


def whatsapp_media(msg: dict) -> list[tuple[str, str]]:
    """Media ``(media_id, suggested_name)`` pairs to download from a message."""
    out: list[tuple[str, str]] = []
    mtype = msg.get("type")
    if mtype not in MEDIA_TYPES:
        return out
    part = msg.get(mtype) or {}
    media_id = part.get("id")
    if not media_id:
        return out
    name = part.get("filename") or ""
    if not name:
        ext = mimetypes.guess_extension((part.get("mime_type") or "").split(";")[0]) or ""
        name = f"{mtype}{ext}"
    out.append((str(media_id), name))
    return out


# ── interactive-button approvals (25d) ────────────────────────────────────────

class WhatsAppApprovalBridge(ApprovalBridgeBase):
    """WhatsApp flavor: interactive *reply buttons* (max 3 — exactly our set)."""

    async def send_buttons(self, client, to, subject: str, detail: str, rid: str) -> None:
        body = chunk_text(f"Approve this action?\n\n{subject}\n{detail}".strip(), 1024)[0]
        payload = {
            "messaging_product": "whatsapp", "to": to, "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": f"ap:once:{rid}", "title": "✅ Allow once"}},
                    {"type": "reply", "reply": {"id": f"ap:always:{rid}", "title": "♾ Always"}},
                    {"type": "reply", "reply": {"id": f"ap:deny:{rid}", "title": "❌ Deny"}},
                ]},
            },
        }
        await client.post(
            f"{GRAPH}/{self.gw.phone_id}/messages",
            headers=self.gw._headers(), json=payload,
        )


# ── the gateway ───────────────────────────────────────────────────────────────

class WhatsAppGateway(Gateway):
    name = "whatsapp"
    token_env = "WHATSAPP_TOKEN"
    owner_env = "WHATSAPP_OWNER_ID"
    webhook_only = True
    id_label = "number"

    def __init__(self, config: Any, deps: Any):
        super().__init__(config, deps)
        self.token = os.getenv("WHATSAPP_TOKEN") or ""
        self.phone_id = os.getenv("WHATSAPP_PHONE_ID") or str(self.conf.get("phone_id") or "")
        self.verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN") or str(self.conf.get("verify_token") or "")
        self.app_secret = os.getenv("WHATSAPP_APP_SECRET") or ""
        self._bridge: WhatsAppApprovalBridge | None = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    # configuration ------------------------------------------------------------

    def validate(self) -> str | None:
        if not self.token or not self.phone_id:
            return "set WHATSAPP_TOKEN and WHATSAPP_PHONE_ID in .env for the WhatsApp gateway"
        return store_guard(self.deps.store)

    def status_info(self) -> dict:
        info = super().status_info()
        tok = self.token
        info["token"] = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "(set)"
        info["phone id"] = self.phone_id or "(unset)"
        return info

    async def run(self) -> None:
        raise RuntimeError(
            "WhatsApp is webhook-only: run `agent --serve` and point Meta's webhook "
            "at https://<host>/webhook/whatsapp (it can't long-poll)."
        )

    def on_mounted(self, client: Any, loop: Any) -> None:
        """Server hook: bind the approval-buttons bridge to the shared loop."""
        if str(self.conf.get("approvals", "buttons")).lower() == "buttons":
            self._bridge = WhatsAppApprovalBridge(self)
            self._bridge.bind(client, loop)

    # webhook authentication (25e) ----------------------------------------------

    def webhook_verify(self, params: dict) -> tuple[int, str] | None:
        """Meta's GET handshake: echo hub.challenge when the verify token matches."""
        def first(key: str) -> str:
            val = params.get(key)
            return val[0] if isinstance(val, list) else (val or "")

        if first("hub.mode") == "subscribe" and first("hub.verify_token") == self.verify_token:
            return (200, first("hub.challenge"))
        return (403, "forbidden")

    def webhook_authorized(self, headers: Any, raw_body: bytes, generic_secret: str | None) -> bool:
        """Verify Meta's ``X-Hub-Signature-256`` (HMAC-SHA256 with the app secret).

        Meta can't send custom headers, so the generic ``WEBHOOK_SECRET`` check
        doesn't apply here. With no app secret configured, posts are accepted
        (the GET verify-token still gates registration) — set
        ``WHATSAPP_APP_SECRET`` in production.
        """
        if not self.app_secret:
            return True
        got = headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(
            self.app_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return len(got) == len(expected) and hmac.compare_digest(got, expected)

    # message handling ---------------------------------------------------------

    async def handle_update(self, client: httpx.AsyncClient, update: dict) -> None:
        assert self._pipeline is not None
        for inbound, to in whatsapp_messages(update):
            try:
                await self._handle_one(client, inbound, to)
            except Exception as exc:  # noqa: BLE001 - keep the webhook healthy
                logger.exception("error handling message: %s", exc)

    async def _handle_one(self, client: httpx.AsyncClient, inbound: Inbound, to: str) -> None:
        # Approval button presses arrive as interactive replies, not prose.
        reply_btn = (inbound.raw.get("interactive") or {}).get("button_reply")
        if reply_btn is not None:
            if self._bridge is not None:
                self._bridge.resolve(reply_btn.get("id"))
            return

        if not self.user_allowed(inbound.user_id):
            if self.monitor is not None:
                self.monitor.on_blocked(inbound.user_id)
            await self.send_message(client, to, self.denied_text(inbound.user_id))
            return

        if self.monitor is not None:
            self.monitor.on_message(inbound.user_id, inbound.user_name, inbound.text)

        reply = self.handle_command(inbound)
        if reply is not None:
            if self.monitor is not None:
                self.monitor.on_reply(True, 0, 0.0, reply)
            await self.send_message(client, to, reply)
            return

        if not self._quota.allowed(inbound.user_id):
            await self.send_message(
                client, to, "You've reached today's message limit. Try again tomorrow."
            )
            return
        self._quota.increment(inbound.user_id)

        await self.send_action(client, inbound)
        if self._bridge is not None:
            self.deps.approval_hook = self._bridge.hook_for(to)
        self.deps.extra["channel_origin"] = {"channel": self.name, "user": inbound.user_id}
        tmp: tempfile.TemporaryDirectory | None = None
        start = time.monotonic()
        ok = True
        try:
            specs = whatsapp_media(inbound.raw)
            if specs:
                tmp = tempfile.TemporaryDirectory(prefix="wa-media-")
                await self._collect_media(client, specs, inbound, tmp.name)
            reply = await self._pipeline.run_turn(inbound)
        except Exception as exc:  # noqa: BLE001 - surface a clean error, stay healthy
            logger.exception("run failed: %s", exc)
            reply = f"Sorry — something went wrong: {exc}"
            ok = False
        finally:
            if tmp is not None:
                tmp.cleanup()
            if self._bridge is not None:
                self.deps.approval_hook = None
            self.deps.extra.pop("channel_origin", None)
        if self.monitor is not None:
            self.monitor.on_reply(ok, self._pipeline.last_tokens, time.monotonic() - start, reply)
        await self.send_message(client, to, reply)

    # inbound media (25c) --------------------------------------------------------

    async def _collect_media(self, client, specs, inbound: Inbound, dest: str) -> None:
        """Download each media file and attach it (vision parts / inlined text)."""
        from ..runtime.attachments import classify_attachment, inline_text_docs, max_mb_from

        max_mb = max_mb_from(self.config.settings)
        docs: list[tuple[str, str]] = []
        for media_id, suggested in specs:
            try:
                path = await self._download_media(client, media_id, suggested, dest)
            except Exception as exc:  # noqa: BLE001 - a failed download drops that file
                logger.warning("could not download media %s: %s", media_id, exc)
                continue
            if not path:
                continue
            kind, val = classify_attachment(path, max_mb)
            if kind == "media":
                inbound.media.append(val)
            elif kind == "text":
                docs.append(val)
        if docs:
            inbound.text = inline_text_docs(inbound.text, docs)

    async def _download_media(self, client, media_id: str, suggested: str, dest: str) -> str | None:
        """Graph flow: ``GET /<media_id>`` → short-lived URL → download the bytes."""
        meta = await client.get(f"{GRAPH}/{media_id}", headers=self._headers())
        meta.raise_for_status()
        url = meta.json().get("url")
        if not url:
            return None
        data = await client.get(url, headers=self._headers())
        data.raise_for_status()
        out = Path(dest) / (suggested or "media")
        out.write_bytes(data.content)
        return str(out)

    # outbound -------------------------------------------------------------------

    async def send_message(self, client: httpx.AsyncClient, to: str, text: str) -> None:
        for chunk in chunk_text(text, MSG_LIMIT):
            payload = {
                "messaging_product": "whatsapp", "to": to, "type": "text",
                "text": {"body": to_whatsapp(chunk)},
            }
            try:
                await client.post(
                    f"{GRAPH}/{self.phone_id}/messages", headers=self._headers(), json=payload
                )
            except Exception as exc:  # noqa: BLE001 - delivery best-effort
                logger.warning("whatsapp send failed: %s", exc)

    async def send_action(self, client: httpx.AsyncClient, inbound: Inbound) -> None:
        """Best-effort: mark the inbound message read + show a typing indicator."""
        msg_id = inbound.raw.get("id")
        if not msg_id:
            return
        payload = {
            "messaging_product": "whatsapp", "status": "read", "message_id": msg_id,
            "typing_indicator": {"type": "text"},
        }
        try:
            await client.post(
                f"{GRAPH}/{self.phone_id}/messages", headers=self._headers(), json=payload
            )
        except Exception:  # noqa: BLE001 - the indicator is non-essential
            pass


GATEWAY = WhatsAppGateway
