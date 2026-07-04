"""Telegram channel — long-poll core (Phase 22b).

Pure ``httpx`` against the Telegram Bot API (just HTTPS GET/POST) — no aiogram,
no python-telegram-bot, no new dependency. Locally this runs as a long-poll loop
(``getUpdates``), which needs no public URL and works behind NAT; the same
:meth:`TelegramGateway.handle_update` is reused by the webhook path on ``--serve``
(Phase 22g).

Each inbound message is gated by the deny-all allowlist, then run through the
shared :class:`~agent.gateways.base.Pipeline` (per-user thread), and the reply is
sent back in ≤4096-char chunks. Token comes from ``TELEGRAM_BOT_TOKEN`` in
``.env``; the channel is dormant until it's set and ``gateways.telegram.enabled``.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from .base import ApprovalBridgeBase, Gateway, Inbound, store_guard
from ..runtime import scheduler

logger = logging.getLogger("agent.gateways.telegram")

API_ROOT = "https://api.telegram.org"
MSG_LIMIT = 4096            # Telegram hard cap on a single sendMessage text
POLL_TIMEOUT = 25          # long-poll seconds held open by getUpdates
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


# ── pure helpers (unit-testable, no IO) ───────────────────────────────────────

def chunk_text(text: str, limit: int = MSG_LIMIT) -> list[str]:
    """Split *text* into ≤*limit* pieces, preferring newline boundaries."""
    text = text or "(no output)"
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


# ── markdown → Telegram formatting ────────────────────────────────────────────
# Models answer in CommonMark-ish markdown (**bold**, `code`, - lists). Telegram
# doesn't render that, so by default we convert to its HTML subset (escaping the
# text), with a plain-text stripper as the fallback / `parse_mode: plain` mode.

_RE_CODE_BLOCK = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_ITALIC = re.compile(r"\*([^*\n]+)\*")
_RE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_RE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*$", re.MULTILINE)
_RE_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_BULLET = "• "


def to_telegram_html(text: str) -> str:
    """Convert the model's markdown to Telegram's HTML subset (safe to send).

    Handles **bold**, *italic*, `code`, ```blocks```, [links](url), # headings and
    - bullets; everything else is HTML-escaped so the message can't break parsing.
    Code spans are pulled out first so their contents aren't reformatted.
    """
    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        blocks.append("<pre>" + _html.escape(m.group(1).rstrip("\n"), quote=False) + "</pre>")
        return f"\x00B{len(blocks) - 1}\x00"

    def _stash_inline(m: re.Match) -> str:
        blocks.append("<code>" + _html.escape(m.group(1), quote=False) + "</code>")
        return f"\x00B{len(blocks) - 1}\x00"

    text = _RE_CODE_BLOCK.sub(_stash_block, text)
    text = _RE_INLINE_CODE.sub(_stash_inline, text)
    # Telegram HTML only needs &, <, > escaped — keep quotes/apostrophes literal.
    text = _html.escape(text, quote=False)
    text = _RE_HEADING.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _RE_BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _RE_ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _RE_LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _RE_BULLET.sub(lambda m: f"{m.group(1)}{_BULLET}", text)
    for i, blk in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", blk)
    return text


def strip_markdown(text: str) -> str:
    """Flatten markdown to clean plain text (no `**`, `#`, `-` left over)."""
    text = _RE_CODE_BLOCK.sub(lambda m: m.group(1), text)
    text = _RE_INLINE_CODE.sub(lambda m: m.group(1), text)
    text = _RE_BOLD.sub(lambda m: m.group(1), text)
    text = _RE_ITALIC.sub(lambda m: m.group(1), text)
    text = _RE_HEADING.sub(lambda m: m.group(1), text)
    text = _RE_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _RE_BULLET.sub(lambda m: f"{m.group(1)}{_BULLET}", text)
    return text


def _render_for(mode: str, text: str) -> tuple[str | None, str]:
    """Return ``(parse_mode, body)`` for a chunk under the configured mode."""
    if mode == "markdownv2":
        return "MarkdownV2", text          # pass-through (caller's responsibility)
    if mode == "plain":
        return None, strip_markdown(text)
    return "HTML", to_telegram_html(text)  # default


def normalize_update(update: dict) -> tuple[int, Inbound] | None:
    """Map a raw Telegram update to ``(chat_id, Inbound)``, or None if not a message.

    Reads text or a media caption; the original message is kept on ``Inbound.raw``
    so the media path (Phase 22d) can pull file ids without re-parsing.
    """
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    frm = msg.get("from") or {}
    user_id = str(frm.get("id") or chat_id)
    text = msg.get("text") or msg.get("caption") or ""
    return chat_id, Inbound(
        user_id=user_id, text=text,
        user_name=frm.get("first_name") or frm.get("username") or "",
        raw=msg,
    )


def telegram_media(msg: dict) -> list[tuple[str, str]]:
    """File ``(file_id, suggested_name)`` pairs to download from a message.

    Covers photos (largest size) and documents — the vision/inline cases the agent
    can actually use. The downloaded file is later classified by extension: images
    and PDFs become multimodal parts; text documents are inlined into the prompt.
    """
    out: list[tuple[str, str]] = []
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        largest = max(photos, key=lambda p: (p.get("width") or 0, p.get("file_size") or 0))
        if largest.get("file_id"):
            out.append((largest["file_id"], "photo.jpg"))
    doc = msg.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        out.append((doc["file_id"], doc.get("file_name") or "document"))
    return out


# ── inline-button approvals (Phase 22h; core shared in base.py) ───────────────

class ApprovalBridge(ApprovalBridgeBase):
    """Telegram flavor: an inline keyboard + ``callback_query`` resolution."""

    async def send_buttons(self, client, chat_id, subject: str, detail: str, rid: str) -> None:
        text = chunk_text(f"Approve this action?\n\n{subject}\n{detail}".strip())[0]
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Allow once", "callback_data": f"ap:once:{rid}"},
            {"text": "♾ Always", "callback_data": f"ap:always:{rid}"},
            {"text": "❌ Deny", "callback_data": f"ap:deny:{rid}"},
        ]]}
        await client.post(
            self.gw._api("sendMessage"),
            json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
        )

    async def handle_callback(self, client: httpx.AsyncClient, callback: dict) -> None:
        """Resolve an approval from a ``callback_query`` and acknowledge it."""
        decision = self.resolve(callback.get("data"))
        if decision is None:
            return
        try:
            await client.post(
                self.gw._api("answerCallbackQuery"),
                json={"callback_query_id": callback.get("id"), "text": decision},
            )
        except Exception:  # noqa: BLE001 - acknowledgement is best-effort
            pass


# ── the gateway ───────────────────────────────────────────────────────────────

class TelegramGateway(Gateway):
    name = "telegram"
    token_env = TOKEN_ENV
    owner_env = "TELEGRAM_OWNER_ID"
    id_label = "Telegram id"

    def __init__(self, config: Any, deps: Any):
        super().__init__(config, deps)
        self.token = os.getenv(TOKEN_ENV) or ""
        self._bridge: ApprovalBridge | None = None
        # Background scheduler (Phase 23): this process can run due jobs (under the
        # shared owner-lock) and deliver results to its users.
        self._scheduler_on = bool((config.settings.get("scheduler") or {}).get("enabled"))
        self._runner_id = scheduler.default_owner_id()
        self._tick_ttl = max(60.0, (POLL_TIMEOUT + 10) * 2)

    # configuration ------------------------------------------------------------

    def validate(self) -> str | None:
        if not self.token:
            return f"set {TOKEN_ENV} in .env to run the Telegram gateway"
        return store_guard(self.deps.store)

    def status_info(self) -> dict:
        info = super().status_info()
        tok = self.token
        info["token"] = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "(set)"
        return info

    def _api(self, method: str) -> str:
        return f"{API_ROOT}/bot{self.token}/{method}"

    def on_mounted(self, client: Any, loop: Any) -> None:
        """Server hook (webhook mode): approval buttons work there too."""
        if str(self.conf.get("approvals", "buttons")).lower() == "buttons":
            self._bridge = ApprovalBridge(self)
            self._bridge.bind(client, loop)

    # transport ----------------------------------------------------------------

    async def run(self) -> None:
        """Long-poll Telegram and run each message through the agent until cancelled."""
        err = self.validate()
        if err:
            raise RuntimeError(err)
        from ..engine.factory import build_agent

        agent = build_agent(self.config)
        self._pipeline = self.make_pipeline(agent)
        logger.info("telegram gateway online (long-poll)")
        async with httpx.AsyncClient(timeout=httpx.Timeout(POLL_TIMEOUT + 10)) as client:
            # Inline-button approvals (opt-in, default on): route confirm-gated
            # tools to the chat instead of refusing them headlessly.
            if str(self.conf.get("approvals", "buttons")).lower() == "buttons":
                self._bridge = ApprovalBridge(self)
                self._bridge.bind(client, asyncio.get_running_loop())
            async with agent:
                await self._poll_loop(client)

    async def _poll_loop(self, client: httpx.AsyncClient) -> None:
        offset: int | None = None
        backoff = 1.0
        while True:
            try:
                updates = await self._get_updates(client, offset)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - transient network/API hiccup
                logger.warning("getUpdates failed (%s) — retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    await self.handle_update(client, update)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad message ≠ dead bot
                    logger.exception("error handling update: %s", exc)
            # Scheduler tick: run due jobs (if we own the lock) + deliver results.
            await self._tick_scheduler(client)

    async def _get_updates(self, client: httpx.AsyncClient, offset: int | None) -> list[dict]:
        params: dict[str, Any] = {"timeout": POLL_TIMEOUT}
        if offset is not None:
            params["offset"] = offset
        resp = await client.get(self._api("getUpdates"), params=params)
        resp.raise_for_status()
        return resp.json().get("result", []) or []

    # message handling ---------------------------------------------------------

    async def handle_update(self, client: httpx.AsyncClient, update: dict) -> None:
        """Gate, run, and reply to a single update. Shared by poll + webhook."""
        # Approval button presses arrive as callback_query, not a message.
        callback = update.get("callback_query")
        if callback is not None:
            if self._bridge is not None:
                await self._bridge.handle_callback(client, callback)
            return

        norm = normalize_update(update)
        if norm is None:
            return
        chat_id, inbound = norm

        if not self.user_allowed(inbound.user_id):
            if self.monitor is not None:
                self.monitor.on_blocked(inbound.user_id)
            await self.send_message(client, chat_id, self.denied_text(inbound.user_id))
            return

        if self.monitor is not None:
            self.monitor.on_message(inbound.user_id, inbound.user_name, inbound.text)

        # Management / convenience commands short-circuit the agent.
        reply = self.handle_command(inbound)
        if reply is not None:
            if self.monitor is not None:
                self.monitor.on_reply(True, 0, 0.0, reply)
            await self.send_message(client, chat_id, reply)
            return

        # Per-user daily quota (token/$ guard) — commands above are exempt.
        if not self._quota.allowed(inbound.user_id):
            await self.send_message(
                client, chat_id,
                "You've reached today's message limit for this bot. Try again tomorrow.",
            )
            return
        self._quota.increment(inbound.user_id)

        assert self._pipeline is not None
        await self.send_action(client, chat_id, "typing")
        # Route this user's tool approvals to inline buttons in their chat.
        if self._bridge is not None:
            self.deps.approval_hook = self._bridge.hook_for(chat_id)
        # Stamp origin so schedule_task records where the job was created.
        self.deps.extra["channel_origin"] = {"channel": self.name, "user": inbound.user_id}
        tmp: tempfile.TemporaryDirectory | None = None
        start = time.monotonic()
        ok = True
        try:
            specs = telegram_media(inbound.raw)
            if specs:
                tmp = tempfile.TemporaryDirectory(prefix="tg-media-")
                await self._collect_media(client, specs, inbound, tmp.name)
            reply = await self._pipeline.run_turn(inbound)
        except Exception as exc:  # noqa: BLE001 - surface a clean error, keep the bot alive
            logger.exception("run failed: %s", exc)
            reply = f"Sorry — something went wrong: {exc}"
            ok = False
        finally:
            if tmp is not None:
                tmp.cleanup()                 # bytes are already in the prompt
            if self._bridge is not None:
                self.deps.approval_hook = None
            self.deps.extra.pop("channel_origin", None)
        if self.monitor is not None:
            self.monitor.on_reply(
                ok, self._pipeline.last_tokens, time.monotonic() - start, reply
            )
        await self.send_message(client, chat_id, reply)

    async def _collect_media(self, client, specs, inbound: Inbound, dest: str) -> None:
        """Download each file and attach it: images/PDF as media, text inlined."""
        from ..runtime.attachments import classify_attachment, inline_text_docs, max_mb_from

        max_mb = max_mb_from(self.config.settings)
        docs: list[tuple[str, str]] = []
        for file_id, suggested in specs:
            path = await self._safe_download(client, file_id, suggested, dest)
            if not path:
                continue
            kind, val = classify_attachment(path, max_mb)
            if kind == "media":
                inbound.media.append(val)
            elif kind == "text":
                docs.append(val)
        if docs:
            inbound.text = inline_text_docs(inbound.text, docs)

    async def _safe_download(self, client, file_id: str, suggested: str, dest: str) -> str | None:
        try:
            return await self._download_file(client, file_id, suggested, dest)
        except Exception as exc:  # noqa: BLE001 - a failed download just drops that file
            logger.warning("could not download %s: %s", file_id, exc)
            return None

    async def _download_file(self, client, file_id: str, suggested: str, dest: str) -> str | None:
        """getFile → download the bytes to *dest*; return the saved path."""
        resp = await client.get(self._api("getFile"), params={"file_id": file_id})
        resp.raise_for_status()
        file_path = (resp.json().get("result") or {}).get("file_path")
        if not file_path:
            return None
        url = f"{API_ROOT}/file/bot{self.token}/{file_path}"
        data = await client.get(url)
        data.raise_for_status()
        name = Path(file_path).name or suggested
        out = Path(dest) / name
        out.write_bytes(data.content)
        return str(out)

    # scheduler (Phase 23) ----------------------------------------------------

    async def _tick_scheduler(self, client: httpx.AsyncClient) -> None:
        """Run due jobs (if we hold the lock) and deliver pending results."""
        if not self._scheduler_on or self._pipeline is None:
            return
        try:
            await scheduler.run_due_jobs(
                self.deps.store, self._pipeline.agent, self.deps,
                owner_id=self._runner_id, ttl=self._tick_ttl,
                usage_limits=getattr(self.config, "usage_limits", None),
                on_fire=self._on_job_fire,
            )
        except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the bot
            logger.warning("scheduler tick failed: %s", exc)
        # Deliver results addressed to telegram → every allowlisted user + owner.
        await self.deliver_pending(client)

    def _on_job_fire(self, job: dict, text: str, ok: bool) -> None:
        if self.monitor is not None:
            self.monitor.on_message("scheduler", "", f"[job {job.get('id')}] {job.get('task')}")
            self.monitor.on_reply(ok, 0, 0.0, text)

    # outbound -----------------------------------------------------------------

    async def send_message(self, client: httpx.AsyncClient, chat_id: int, text: str) -> None:
        mode = str(self.conf.get("parse_mode", "html")).lower()
        for chunk in chunk_text(text):
            parse_mode, body = _render_for(mode, chunk)
            payload: dict[str, Any] = {"chat_id": chat_id, "text": body}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            resp = await self._post_with_backoff(client, "sendMessage", payload)
            # If Telegram rejects our formatting (400 — bad entities), resend the
            # chunk as plain text so the message still gets through.
            if resp is not None and resp.status_code == 400 and parse_mode:
                await self._post_with_backoff(
                    client, "sendMessage", {"chat_id": chat_id, "text": strip_markdown(chunk)}
                )

    async def _post_with_backoff(self, client, method: str, payload: dict):
        """POST once, honoring a single Telegram 429 ``retry_after`` flood wait.

        Returns the final response (or None if the request raised), so the caller
        can react to a 400 formatting rejection.
        """
        try:
            resp = await client.post(self._api(method), json=payload)
            if resp.status_code == 429:
                retry = 1.0
                try:
                    retry = float(resp.json().get("parameters", {}).get("retry_after", 1))
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(min(retry, 30))
                resp = await client.post(self._api(method), json=payload)
            return resp
        except Exception as exc:  # noqa: BLE001 - delivery best-effort
            logger.warning("%s failed: %s", method, exc)
            return None

    async def send_action(self, client: httpx.AsyncClient, chat_id: int, action: str) -> None:
        try:
            await client.post(self._api("sendChatAction"), json={"chat_id": chat_id, "action": action})
        except Exception:  # noqa: BLE001 - a typing indicator is non-essential
            pass


GATEWAY = TelegramGateway
