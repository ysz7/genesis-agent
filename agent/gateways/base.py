"""Gateway base — the shared inbound→agent→outbound core (Phase 22a).

Every channel (Telegram, WhatsApp, …) is a :class:`Gateway` that owns only its
transport (long-poll loop or webhook handler) and message formatting. The actual
agent work — access control, guardrails, per-user memory, the model run — is
identical across channels and lives here, so a new channel is ~100 lines of API
glue and nothing more.

Key invariants (locked design decisions):

- **Per-user memory.** Each platform user maps to its own persistent thread,
  ``session = "<gateway>:<user_id>"`` (Phase 18). Conversations are isolated and
  survive a restart, like Chatwoot's per-contact inbox.
- **Concurrent store.** The CLI and a gateway subprocess share one ``store``, so a
  gateway requires the SQLite/WAL backend; :func:`store_guard` enforces it.
- **Deny-all access.** An empty allowlist means *nobody* — a stranger who finds
  the bot can't spend your tokens. See :class:`AccessControl`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..runtime.store import SQLiteStore

logger = logging.getLogger("agent.gateways")


# ── normalized message shapes ─────────────────────────────────────────────────

@dataclass
class Inbound:
    """A platform message normalized to what the pipeline needs."""

    user_id: str                       # platform user/chat id (string-keyed)
    text: str = ""                     # message text (may be empty with media)
    media: list[str] = field(default_factory=list)  # local paths / URLs to attach
    user_name: str = ""                # display name, for logs/greetings
    raw: dict = field(default_factory=dict)         # original platform payload


@dataclass
class Outbound:
    """A reply to send back to the platform."""

    text: str


# ── settings helpers ──────────────────────────────────────────────────────────

def gateway_settings(settings: dict, name: str) -> dict:
    """The ``gateways.<name>`` block from settings, or ``{}``."""
    block = settings.get("gateways") or {}
    if not isinstance(block, dict):
        return {}
    conf = block.get(name) or {}
    return conf if isinstance(conf, dict) else {}


def gateway_enabled(settings: dict, name: str) -> bool:
    """True when ``gateways.<name>.enabled`` is set."""
    return bool(gateway_settings(settings, name).get("enabled"))


def any_gateway_enabled(settings: dict) -> bool:
    """True when at least one channel under ``gateways:`` is enabled."""
    block = settings.get("gateways") or {}
    if not isinstance(block, dict):
        return False
    return any(bool((v or {}).get("enabled")) for v in block.values() if isinstance(v, dict))


# ── concurrent-store guard ────────────────────────────────────────────────────

def store_is_concurrent(store: Any) -> bool:
    """True when the store is safe for the CLI and a gateway writing at once."""
    return isinstance(store, SQLiteStore)


def store_guard(store: Any) -> str | None:
    """Return an error message if *store* is unsafe for gateways, else ``None``."""
    if store_is_concurrent(store):
        return None
    return (
        "gateways need a concurrent store. Set `store: agent.sqlite` in "
        "settings.yaml (SQLite/WAL) — a JSON store is not safe when the CLI and a "
        "gateway write to it at the same time."
    )


# ── access control (deny-all allowlist) ───────────────────────────────────────

class AccessControl:
    """Allowlist gate for a channel: empty ⇒ deny everyone.

    The effective allowlist is the union of ``gateways.<name>.allowlist`` from
    settings (the seed) and any ids added at runtime via :meth:`allow` (persisted
    in the store under ``gw:<name>:allow``). ``/allow`` and ``/deny`` only ever
    touch the runtime set; a settings-seeded id can't be revoked live.
    """

    def __init__(self, store: Any, name: str, seed: list | None = None):
        self.store = store
        self.key = f"gw:{name}:allow"
        self._seed = [str(x) for x in (seed or [])]

    def _added(self) -> list[str]:
        return [str(x) for x in (self.store.get(self.key, []) or [])]

    def listing(self) -> list[str]:
        """Effective allowlist (seed first, then runtime additions; de-duped)."""
        return list(dict.fromkeys(self._seed + self._added()))

    def allowed(self, user_id: Any) -> bool:
        """True only if *user_id* is on the (non-empty) effective allowlist."""
        return str(user_id) in set(self.listing())

    def allow(self, user_id: Any) -> None:
        """Grant *user_id* access (persisted)."""
        added = self._added()
        if str(user_id) not in added:
            added.append(str(user_id))
            self.store.set(self.key, added)

    def deny(self, user_id: Any) -> bool:
        """Revoke a runtime-added id. Returns False if it was seeded in settings."""
        uid = str(user_id)
        added = self._added()
        if uid in added:
            self.store.set(self.key, [i for i in added if i != uid])
            return True
        return uid not in self._seed  # True = nothing to do; False = can't revoke seed


# ── per-user daily quota ──────────────────────────────────────────────────────

class Quota:
    """Per-user, per-day message cap — a simple token/$ guard.

    ``per_day <= 0`` means unlimited (the default). Counts live in the store under
    ``gw:<name>:quota:<user>:<yyyy-mm-dd>``, so they reset naturally each day and
    are shared across the gateway's processes.
    """

    def __init__(self, store: Any, name: str, per_day: Any = 0):
        self.store = store
        self.name = name
        try:
            self.per_day = int(per_day or 0)
        except (TypeError, ValueError):
            self.per_day = 0

    def _key(self, user_id: Any) -> str:
        from datetime import date

        return f"gw:{self.name}:quota:{user_id}:{date.today().isoformat()}"

    def allowed(self, user_id: Any) -> bool:
        """True if *user_id* is under today's cap (always True when unlimited)."""
        if self.per_day <= 0:
            return True
        return int(self.store.get(self._key(user_id), 0) or 0) < self.per_day

    def increment(self, user_id: Any) -> None:
        """Count one message against *user_id*'s daily quota."""
        if self.per_day <= 0:
            return
        key = self._key(user_id)
        self.store.set(key, int(self.store.get(key, 0) or 0) + 1)


# ── the shared pipeline ───────────────────────────────────────────────────────

def _as_text(output: Any) -> str:
    """A channel sends text; coerce a structured output to a readable string."""
    if isinstance(output, str):
        return output
    if hasattr(output, "model_dump_json"):
        try:
            return output.model_dump_json(indent=2)
        except Exception:  # noqa: BLE001
            pass
    return str(output)


def _tokens(result: Any) -> int:
    """Total tokens for a run (input + output), best-effort (mirrors the server)."""
    try:
        usage = result.usage
        usage = usage if hasattr(usage, "input_tokens") else usage()
        return (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


class Pipeline:
    """Inbound→agent→outbound core, reused by every gateway.

    Runs the message through input guardrails, this user's persistent thread, and
    the agent, then returns the reply text. Decoupled from :class:`Config` (takes a
    plain settings dict) so it's trivial to unit-test with a fake agent + store.
    """

    def __init__(self, name: str, agent: Any, deps: Any, settings: dict, *, usage_limits: Any = None):
        self.name = name
        self.agent = agent
        self.deps = deps
        self.settings = settings
        self.usage_limits = usage_limits
        self.keep = int(settings.get("history_keep", 40))
        self.last_tokens = 0          # tokens of the most recent run (for the monitor)

    def session_for(self, user_id: Any) -> str:
        """The persistent-thread id for a platform user: ``<gateway>:<user_id>``."""
        return f"{self.name}:{user_id}"

    async def run_turn(self, inbound: Inbound) -> str:
        """Run one inbound message and return the reply text.

        Loads the user's thread, runs the agent with it, saves it back. Input
        guardrails may short-circuit with a refusal/redaction message.
        """
        from ..engine import guardrails
        from ..runtime import threads
        from ..runtime.attachments import build_user_prompt, max_mb_from, prompt_text
        from ..runtime.transcripts import write_transcript

        allowed, text = guardrails.check_input(self.settings, inbound.text)
        if not allowed:
            return text

        session = self.session_for(inbound.user_id)
        history = threads.load_thread(self.deps.store, session)
        if inbound.media:
            prompt: Any = build_user_prompt(
                text or "(see attached)", inbound.media,
                allow_local=True, max_mb=max_mb_from(self.settings),
            )
        else:
            prompt = text

        start = time.monotonic()
        ok = True
        error: str | None = None
        result = None
        try:
            result = await self.agent.run(
                prompt, deps=self.deps,
                message_history=history or None,
                usage_limits=self.usage_limits,
            )
            self.last_tokens = _tokens(result)
            threads.save_thread(self.deps.store, session, result.all_messages(), keep=self.keep)
            return _as_text(result.output)
        except Exception as exc:  # noqa: BLE001 - the caller decides how to surface this
            ok = False
            error = str(exc)
            raise
        finally:
            write_transcript(
                self.deps, prompt_text(prompt), result=result,
                duration=time.monotonic() - start, ok=ok, error=error,
            )


# ── approval bridge core (shared button-approvals, Phase 22h/25d) ─────────────

class ApprovalBridgeBase:
    """Routes a tool approval to a chat as Allow / Always / Deny buttons.

    The agent's confirm gate is a **synchronous** ``deps.approval_hook`` invoked
    from inside a tool (Pydantic AI runs sync tools in a worker thread). From that
    thread we send the buttons on the gateway's event loop, then block on a
    ``threading.Event`` until the channel's callback arrives or a timeout elapses
    — on timeout the decision is ``"deny"``. Returns ``once|always|deny``, exactly
    the hook contract.

    Subclasses implement :meth:`send_buttons` (channel-specific UI) and call
    :meth:`resolve` from their inbound callback parsing. The ``ap:<decision>:<rid>``
    payload format is shared so both sides agree.
    """

    def __init__(self, gateway: "Gateway"):
        self.gw = gateway
        self.timeout = float(gateway.conf.get("approval_timeout", 120))
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, client: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._client, self._loop = client, loop

    def hook_for(self, recipient: Any):
        """A sync ``(subject, detail) -> decision`` hook bound to *recipient*."""
        def hook(subject: str, detail: str) -> str:
            return self._request(recipient, subject, detail)
        return hook

    def _request(self, recipient: Any, subject: str, detail: str) -> str:
        if self._loop is None or self._client is None:
            return "deny"
        rid = uuid.uuid4().hex[:8]
        event = threading.Event()
        with self._lock:
            self._pending[rid] = {"event": event, "decision": "deny"}
        asyncio.run_coroutine_threadsafe(
            self._safe_send(recipient, subject, detail, rid), self._loop
        )
        event.wait(self.timeout)
        with self._lock:
            return self._pending.pop(rid, {}).get("decision", "deny")

    async def _safe_send(self, recipient: Any, subject: str, detail: str, rid: str) -> None:
        try:
            await self.send_buttons(self._client, recipient, subject, detail, rid)
        except Exception as exc:  # noqa: BLE001 - a failed prompt just denies on timeout
            logger.warning("could not send approval prompt: %s", exc)

    def resolve(self, payload: str) -> str | None:
        """Resolve a callback payload ``ap:<decision>:<rid>``; returns the decision."""
        parts = str(payload or "").split(":")
        if len(parts) != 3 or parts[0] != "ap":
            return None
        _, decision, rid = parts
        with self._lock:
            entry = self._pending.get(rid)
            if entry is not None:
                entry["decision"] = decision
                entry["event"].set()
        return decision

    async def send_buttons(self, client: Any, recipient: Any, subject: str, detail: str, rid: str) -> None:
        """Send the Allow once / Always / Deny prompt (subclasses implement)."""
        raise NotImplementedError


# ── gateway base ──────────────────────────────────────────────────────────────

class Gateway(ABC):
    """Base for a messaging channel. Subclasses own transport + formatting only.

    Set the class attribute ``name`` (e.g. ``"telegram"``). Override
    :meth:`validate` to check the token/config, and :meth:`run` to drive the
    transport loop — building a :class:`Pipeline` via :meth:`make_pipeline`.
    """

    name: str = ""
    # .env keys the menu offers to edit for this channel (credential + owner id).
    # Empty means the channel has no such key.
    token_env: str = ""
    owner_env: str = ""
    # True for channels with no local transport (no long-poll): they run only as
    # webhook routes under `--serve`. The menu hides Start/Stop for these.
    webhook_only: bool = False
    # How this channel names a user in refusals/commands ("Telegram id", "number").
    id_label: str = "id"

    def __init__(self, config: Any, deps: Any):
        self.config = config
        self.deps = deps
        self.conf = gateway_settings(config.settings, self.name)
        self.access = AccessControl(deps.store, self.name, self.conf.get("allowlist"))
        # The owner is always allowed (bootstrap) and is the only user who may run
        # management commands. From settings `owner_id` or the channel's env var.
        self.owner_id = str(
            self.conf.get("owner_id")
            or (os.getenv(self.owner_env) if self.owner_env else "")
            or ""
        )
        self._quota = Quota(deps.store, self.name, self.conf.get("max_messages_per_day"))
        self._pipeline: Pipeline | None = None
        # Optional rich live feed (set by the CLI on a console); None = headless.
        self.monitor: Any = None

    def status_info(self) -> dict:
        """Banner fields for the monitor. Subclasses add channel-specific keys."""
        info = {
            "allowed": len(self.access.listing()),
            "store": getattr(getattr(self.deps, "store", None), "path", ""),
            "model": getattr(self.config, "model", ""),
        }
        if self.owner_id:
            info["owner"] = self.owner_id
        return info

    # ── access + management commands (shared by every channel) ────────────────

    def is_owner(self, user_id: Any) -> bool:
        return bool(self.owner_id) and str(user_id) == self.owner_id

    def user_allowed(self, user_id: Any) -> bool:
        """Deny-all allowlist plus the owner bootstrap."""
        return self.access.allowed(user_id) or self.is_owner(user_id)

    def denied_text(self, user_id: Any) -> str:
        return (
            "Access is closed for this bot.\n"
            f"Your {self.id_label} is {user_id} — ask the owner to allow it."
        )

    def handle_command(self, inbound: "Inbound") -> str | None:
        """Return a reply for a ``/command``, or None to let the agent handle it.

        ``/whoami`` and ``/help`` are for any allowed user; ``/allow``, ``/deny``
        and ``/allowlist`` are owner-only and manage the deny-all allowlist live.
        An unrecognized ``/command`` falls through to the agent (returns None).
        """
        text = (inbound.text or "").strip()
        if not text.startswith("/"):
            return None
        parts = text.split()
        cmd = parts[0][1:].split("@", 1)[0].lower()   # strip leading / and @BotName
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "whoami":
            return f"Your {self.id_label}: {inbound.user_id}"
        if cmd in ("start", "help"):
            extra = " · /allow <id> · /deny <id> · /allowlist" if self.is_owner(inbound.user_id) else ""
            return f"Commands: /whoami{extra}\nSend me a message and I'll answer."
        if cmd in ("allow", "deny", "allowlist", "allowed"):
            if not self.is_owner(inbound.user_id):
                return "Owner only."
            if cmd == "allow":
                if not arg:
                    return "usage: /allow <id>"
                self.access.allow(arg)
                return f"Allowed {arg}."
            if cmd == "deny":
                if not arg:
                    return "usage: /deny <id>"
                ok = self.access.deny(arg)
                return f"Removed {arg}." if ok else f"Cannot revoke {arg} — it is seeded in settings.yaml."
            ids = self.access.listing()
            return "Allowed ids: " + (", ".join(ids) if ids else "(none)")
        return None

    # ── scheduled-result delivery (Phase 23/25a) ───────────────────────────────

    def delivery_recipients(self) -> list[str]:
        """Who receives scheduled-task results: every allowlisted user + owner."""
        ids = [str(i) for i in self.access.listing()]
        if self.owner_id and self.owner_id not in ids:
            ids.append(self.owner_id)
        return ids

    async def deliver_pending(self, client: Any) -> None:
        """Send this channel's pending scheduled-task results and mark them.

        Called from the channel's own loop (Telegram long-poll) or the server's
        scheduler ticker (webhook-only channels like WhatsApp). The per-channel
        ``consumed`` marker makes delivery at-most-once per channel.
        """
        from ..runtime import scheduler

        store = self.deps.store
        recipients = self.delivery_recipients()
        for rec in scheduler.pending_for(store, self.name):
            for recipient in recipients:
                await self.send_message(client, recipient, f"⏰ {rec['text']}")
            scheduler.mark_delivered(store, rec["id"], self.name)

    async def send_message(self, client: Any, recipient: Any, text: str) -> None:
        """Send *text* to *recipient* on this channel (subclasses implement)."""
        raise NotImplementedError

    def validate(self) -> str | None:
        """Return an error message if the channel is misconfigured, else ``None``."""
        return store_guard(self.deps.store)

    def make_pipeline(self, agent: Any) -> Pipeline:
        """A :class:`Pipeline` bound to *agent* and this channel's name."""
        return Pipeline(
            self.name, agent, self.deps, self.config.settings,
            usage_limits=getattr(self.config, "usage_limits", None),
        )

    def webhook_verify(self, params: dict) -> tuple[int, str] | None:
        """Optional ``GET /webhook/<name>`` handshake (e.g. Meta's challenge).

        Returns ``(status, body)`` to answer, or ``None`` if the channel has no
        GET handshake (Telegram uses POST only).
        """
        return None

    def webhook_authorized(self, headers: Any, raw_body: bytes, generic_secret: str | None) -> bool:
        """Authenticate an inbound ``POST /webhook/<name>`` (channel-specific).

        Default: when ``WEBHOOK_SECRET`` is configured, require the platform's
        secret header to match (Telegram sends it back as
        ``X-Telegram-Bot-Api-Secret-Token`` when registered with ``secret_token``).
        Channels with their own signing scheme (Meta's ``X-Hub-Signature-256``)
        override this.
        """
        if not generic_secret:
            return True
        import hmac as _hmac

        got = headers.get("x-telegram-bot-api-secret-token", "")
        return len(got) == len(generic_secret) and _hmac.compare_digest(got, generic_secret)

    def on_mounted(self, client: Any, loop: Any) -> None:
        """Server hook: called once when mounted as a webhook on ``--serve``.

        Channels use it to bind resources that need the shared client/loop (e.g.
        the approval-buttons bridge). Default: nothing.
        """

    @abstractmethod
    async def run(self) -> None:
        """Drive the transport until cancelled (long-poll loop or server)."""
        raise NotImplementedError
