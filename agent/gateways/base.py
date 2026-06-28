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

import logging
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
        from ..runtime.attachments import build_user_prompt, max_mb_from

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

        result = await self.agent.run(
            prompt, deps=self.deps,
            message_history=history or None,
            usage_limits=self.usage_limits,
        )
        self.last_tokens = _tokens(result)
        threads.save_thread(self.deps.store, session, result.all_messages(), keep=self.keep)
        return _as_text(result.output)


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

    def __init__(self, config: Any, deps: Any):
        self.config = config
        self.deps = deps
        self.conf = gateway_settings(config.settings, self.name)
        self.access = AccessControl(deps.store, self.name, self.conf.get("allowlist"))
        # Optional rich live feed (set by the CLI on a console); None = headless.
        self.monitor: Any = None

    def status_info(self) -> dict:
        """Banner fields for the monitor. Subclasses add channel-specific keys."""
        return {
            "allowed": len(self.access.listing()),
            "store": getattr(getattr(self.deps, "store", None), "path", ""),
            "model": getattr(self.config, "model", ""),
        }

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

    @abstractmethod
    async def run(self) -> None:
        """Drive the transport until cancelled (long-poll loop or server)."""
        raise NotImplementedError
