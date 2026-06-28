"""WhatsApp channel — Meta Cloud API, webhook-only (Phase 22j).

WhatsApp has no long-poll; Meta delivers messages to a public webhook. So this
channel only runs under ``agent --serve`` (it mounts at ``POST /webhook/whatsapp``
like every gateway, plus the ``GET`` verification handshake Meta requires). Pure
``httpx`` against the Graph API — no SDK.

Inbound text (and media captions) run through the shared
:class:`~agent.gateways.base.Pipeline` with a per-sender thread
(``whatsapp:<phone>``); replies go out via the Graph ``/messages`` endpoint in
≤4096-char chunks. Access is deny-all by phone number, same as Telegram. Media
download and inline-button approvals are intentionally out of scope here (v1);
approval-gated tools fall back to the headless refusal.

Secrets in ``.env``: ``WHATSAPP_TOKEN`` (Graph API token), ``WHATSAPP_PHONE_ID``
(the sender phone-number id), ``WHATSAPP_VERIFY_TOKEN`` (the webhook handshake
string you choose in the Meta dashboard).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .base import Gateway, Inbound, Quota, store_guard
from .telegram import chunk_text, strip_markdown

logger = logging.getLogger("agent.gateways.whatsapp")

GRAPH = "https://graph.facebook.com/v19.0"


def whatsapp_messages(update: dict) -> list[tuple[Inbound, str]]:
    """Extract ``(Inbound, to)`` pairs from a Meta webhook payload.

    Meta nests messages under ``entry[].changes[].value.messages[]``; status
    callbacks (delivered/read) carry no ``messages`` and yield nothing.
    """
    out: list[tuple[Inbound, str]] = []
    for entry in update.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for msg in value.get("messages") or []:
                sender = msg.get("from")
                if not sender:
                    continue
                mtype = msg.get("type")
                if mtype == "text":
                    text = (msg.get("text") or {}).get("body", "")
                else:
                    text = (msg.get(mtype) or {}).get("caption", "") or f"[{mtype} message]"
                out.append((Inbound(user_id=str(sender), text=text, raw=msg), str(sender)))
    return out


class WhatsAppGateway(Gateway):
    name = "whatsapp"
    token_env = "WHATSAPP_TOKEN"

    def __init__(self, config: Any, deps: Any):
        super().__init__(config, deps)
        self.token = os.getenv("WHATSAPP_TOKEN") or ""
        self.phone_id = os.getenv("WHATSAPP_PHONE_ID") or str(self.conf.get("phone_id") or "")
        self.verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN") or str(self.conf.get("verify_token") or "")
        self._quota = Quota(deps.store, self.name, self.conf.get("max_messages_per_day"))

    def validate(self) -> str | None:
        if not self.token or not self.phone_id:
            return "set WHATSAPP_TOKEN and WHATSAPP_PHONE_ID in .env for the WhatsApp gateway"
        return store_guard(self.deps.store)

    async def run(self) -> None:
        raise RuntimeError(
            "WhatsApp is webhook-only: run `agent --serve` and point Meta's webhook "
            "at https://<host>/webhook/whatsapp (it can't long-poll)."
        )

    def webhook_verify(self, params: dict) -> tuple[int, str] | None:
        """Meta's GET handshake: echo hub.challenge when the verify token matches."""
        def first(key: str) -> str:
            val = params.get(key)
            return val[0] if isinstance(val, list) else (val or "")

        if first("hub.mode") == "subscribe" and first("hub.verify_token") == self.verify_token:
            return (200, first("hub.challenge"))
        return (403, "forbidden")

    async def handle_update(self, client: httpx.AsyncClient, update: dict) -> None:
        assert self._pipeline is not None
        for inbound, to in whatsapp_messages(update):
            if not self.access.allowed(inbound.user_id):
                await self.send_message(client, to, self._denied_text(inbound.user_id))
                continue
            if not self._quota.allowed(inbound.user_id):
                await self.send_message(client, to, "You've reached today's message limit. Try again tomorrow.")
                continue
            self._quota.increment(inbound.user_id)
            try:
                reply = await self._pipeline.run_turn(inbound)
            except Exception as exc:  # noqa: BLE001 - keep the webhook healthy
                logger.exception("run failed: %s", exc)
                reply = f"Sorry — something went wrong: {exc}"
            await self.send_message(client, to, reply)

    def _denied_text(self, user_id: str) -> str:
        return f"Access is closed for this bot. Your number is {user_id}; ask the owner to allow it."

    async def send_message(self, client: httpx.AsyncClient, to: str, text: str) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        for chunk in chunk_text(text):
            body = strip_markdown(chunk)        # WhatsApp doesn't render CommonMark
            payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
            try:
                await client.post(f"{GRAPH}/{self.phone_id}/messages", headers=headers, json=payload)
            except Exception as exc:  # noqa: BLE001 - delivery best-effort
                logger.warning("whatsapp send failed: %s", exc)


GATEWAY = WhatsAppGateway
