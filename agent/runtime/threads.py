"""Phase 18: persistent conversation threads over the existing state store.

A conversation (the REPL's running ``message_history``, or a server caller's
session) is serialized and parked under a ``session_id`` in the same ``store``
the agent already uses — JSON by default, SQLite for larger/concurrent state.
**No new dependency:** Pydantic AI messages round-trip through their own type
adapter (``ModelMessagesTypeAdapter``) + ``to_jsonable_python``.

Opt-in via ``settings.yaml``::

    threads:
      enabled: true

Resilience is built in: a missing, corrupt, or unreadable thread blob degrades
to a fresh (empty) conversation with a logged warning — never a crash. Threads
live in ``workspace/`` (the sandbox), like everything else the agent persists.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from pydantic_ai.direct import model_request
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    TextPart,
    UserPromptPart,
)
from pydantic_core import to_jsonable_python

from .store import Store

logger = logging.getLogger("agent.threads")

_PREFIX = "thread:"       # one blob per session: thread:<id>
_INDEX = "threads:index"  # list of known session ids (for /threads)
_META = "threads:meta"    # {id: {title, updated_at, channel, msg_count}} (Phase 36)

# Auto-titling (Phase 37): one short, context-derived title per session.
_TITLE_MAX_CHARS = 60     # a browser row, not a paragraph
_TITLE_MAX_TOKENS = 24    # the side-call is deliberately tiny
_TITLE_PROMPT = (
    "Write a short, specific title for a conversation that starts with the "
    "message below. Use 3-6 words, no quotes, no trailing punctuation, plain "
    "text only. Reply with the title and nothing else.\n\nMESSAGE:\n"
)


def enabled(settings: dict) -> bool:
    """True when ``threads.enabled`` is set in settings."""
    return bool((settings.get("threads") or {}).get("enabled"))


def _key(session_id: str) -> str:
    return f"{_PREFIX}{session_id}"


def _now() -> str:
    """UTC timestamp for ``updated_at`` (second precision, ISO 8601)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def relative_time(iso: str | None) -> str:
    """Human 'time since' for an ISO-8601 UTC stamp — the session browser's clock.

    Lives here (dependency-free) so both the rich CLI *and* the headless gateways
    can label sessions without importing the console layer. Returns ``—`` when the
    timestamp is missing or unparseable (e.g. a legacy thread with no ``updated_at``),
    so nothing crashes on old metadata.
    """
    if not iso:
        return "—"
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "—"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    secs = int(max((datetime.now(timezone.utc) - then).total_seconds(), 0))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        return f"{days // 7}w ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def save_thread(
    store: Store,
    session_id: str,
    messages: list,
    keep: int | None = None,
    *,
    channel: str | None = None,
) -> None:
    """Persist *messages* (a Pydantic AI history) under *session_id*.

    *keep*, when given, trims to the last N messages so a long-running thread
    stays lean on disk (mirrors the REPL's ``history_keep`` cap). Serialization
    failures are logged and swallowed — persistence must never break a run.

    *channel* (``cli`` / a gateway name / ``server``) records where the session
    lives, so the cross-channel session browser (Phase 38/39) can label it.

    The per-thread metadata map (``threads:meta``, Phase 36) is updated here in
    one place, so every writer (REPL, server, Telegram, WhatsApp) populates it
    uniformly without per-channel code: ``updated_at`` and ``msg_count`` refresh
    on every save, ``channel`` is recorded, and ``title`` is left for Phase 37.
    """
    msgs = list(messages)
    if keep is not None and keep > 0 and len(msgs) > keep:
        msgs = msgs[-keep:]
    try:
        data = to_jsonable_python(msgs)
    except Exception as exc:  # noqa: BLE001 - never let a save break the run
        logger.warning("could not serialize thread %r: %s", session_id, exc)
        return
    store.set(_key(session_id), data)
    index = store.get(_INDEX, []) or []
    if session_id not in index:
        index.append(session_id)
        store.set(_INDEX, index)

    meta = store.get(_META, {}) or {}
    entry = dict(meta.get(session_id) or {})
    entry.setdefault("title", "")
    entry["updated_at"] = _now()
    entry["msg_count"] = len(msgs)
    if channel:
        entry["channel"] = channel
    else:
        entry.setdefault("channel", "")
    meta[session_id] = entry
    store.set(_META, meta)


def thread_meta(store: Store) -> dict[str, dict]:
    """The per-thread metadata map, migrating legacy index-only ids on first read.

    Threads saved before Phase 36 exist only in the flat ``threads:index`` with no
    metadata. On first read they're folded into ``threads:meta`` with a minimal
    entry (empty title, unknown ``updated_at``) and persisted — back-compat with no
    crash, matching Phase 18's resilience posture. Idempotent: a second read finds
    nothing to migrate.
    """
    meta = dict(store.get(_META, {}) or {})
    changed = False
    for session_id in store.get(_INDEX, []) or []:
        if session_id not in meta:
            meta[session_id] = {
                "title": "",
                "updated_at": None,
                "channel": "",
                "msg_count": 0,
            }
            changed = True
    if changed:
        store.set(_META, meta)
    return meta


def sessions_by_recency(store: Store) -> list[dict]:
    """Every saved session as a metadata dict (``id`` included), newest-used first.

    Reads through :func:`thread_meta`, so the legacy flat index is migrated first
    and sessions from **every** channel (CLI, server, gateways) appear in one list —
    the input the cross-channel session browser (Phase 38/39) renders. Sorted by
    ``updated_at`` descending; entries with no timestamp (legacy, never re-saved)
    fall to the end, ``id`` breaking ties for a stable order.
    """
    meta = thread_meta(store)
    rows = [{"id": sid, **entry} for sid, entry in meta.items()]
    rows.sort(key=lambda r: (r.get("updated_at") or "", r["id"]), reverse=True)
    return rows


def most_recent_session(store: Store) -> str | None:
    """The id of the most-recently-used session, or ``None`` when there are none.

    What "Chat" resumes so the CLI drops back into where you left off (Phase 38);
    ``None`` means start fresh (no sessions yet).
    """
    rows = sessions_by_recency(store)
    return rows[0]["id"] if rows else None


def resume_target(store: Store, settings: dict, session_id: str | None = None) -> str | None:
    """Which session "Chat" should open (Phase 38).

    An explicit *session_id* wins (the manager picked one); otherwise, with threads
    enabled, resume the most-recently-used session, falling back to ``None`` — a
    fresh, ephemeral REPL — when threads are off or none are saved yet.
    """
    if session_id is not None:
        return session_id
    if not enabled(settings):
        return None
    return most_recent_session(store)


def rename_thread(store: Store, session_id: str, title: str) -> None:
    """Set a session's stored title (the session manager's rename, Phase 38).

    Writes into ``threads:meta`` without touching the saved conversation blob; a
    session that has no meta entry yet gets one so an id-only legacy thread can be
    titled by hand.
    """
    meta = store.get(_META, {}) or {}
    entry = dict(meta.get(session_id) or {})
    entry["title"] = title.strip()
    meta[session_id] = entry
    store.set(_META, meta)


# ── Gateway per-user sessions (Phase 39) ─────────────────────────────────────
# A gateway user holds several sessions under ``<gateway>:<user_id>:<slug>``, with
# an active-session pointer ``active:<gateway>:<user_id>`` → the current slug. The
# *prefix* (``<gateway>:<user_id>``) keys everything; helpers take it so the same
# code serves every channel. Off (threads disabled) the gateway keeps its single
# rolling ``<gateway>:<user_id>`` thread — today's behaviour.

def _active_key(prefix: str) -> str:
    return f"active:{prefix}"


def _sid(prefix: str, slug: str) -> str:
    return f"{prefix}:{slug}"


def _slugify(name: str) -> str:
    """A safe, short slug from a user-supplied name (empty when nothing usable)."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")[:32]


def user_sessions(store: Store, prefix: str) -> list[dict]:
    """This gateway user's sessions (metadata dicts + ``id``/``slug``), newest first.

    Filters the global recency list to ids under ``<prefix>:`` — the trailing
    separator keeps ``telegram:12`` from matching ``telegram:123:main``.
    """
    rows = []
    for r in sessions_by_recency(store):
        if r["id"].startswith(prefix + ":"):
            rows.append({**r, "slug": r["id"][len(prefix) + 1:]})
    return rows


def active_slug(store: Store, prefix: str) -> str | None:
    """The current active-session slug for a gateway user, or ``None`` if unset."""
    return store.get(_active_key(prefix)) or None


def _adopt_legacy_thread(store: Store, prefix: str, slug: str) -> None:
    """Fold a pre-Phase-39 single thread (``<prefix>``) into ``<prefix>:<slug>``.

    So enabling multi-session on an existing bot doesn't orphan a user's history:
    the blob, index entry, and meta entry move under the new slugged id (a move,
    not a copy — the old id is retired). No-op when there's nothing to adopt.
    """
    legacy_key = _key(prefix)
    blob = store.get(legacy_key)
    sid = _sid(prefix, slug)
    if not blob or store.get(_key(sid)):
        return
    store.set(_key(sid), blob)
    store.delete(legacy_key)
    index = store.get(_INDEX, []) or []
    if prefix in index:
        store.set(_INDEX, [sid if x == prefix else x for x in index])
    meta = store.get(_META, {}) or {}
    if prefix in meta and sid not in meta:
        meta[sid] = meta.pop(prefix)
        store.set(_META, meta)


def active_session(store: Store, prefix: str) -> str:
    """The active session id for a gateway user, defaulting sanely on first use.

    With no pointer yet, adopts a legacy single-thread as ``<prefix>:main`` (so an
    upgrade keeps the user's history), else starts a fresh ``main`` — and persists
    the pointer so subsequent turns land in the same place.
    """
    slug = store.get(_active_key(prefix))
    if not slug:
        slug = "main"
        store.set(_active_key(prefix), slug)
        _adopt_legacy_thread(store, prefix, slug)
        # Make the default session visible in /sessions right away (adoption may
        # already have moved a meta entry across, in which case this is a no-op).
        _ensure_meta(store, _sid(prefix, slug), prefix.split(":", 1)[0])
    return _sid(prefix, slug)


def _ensure_meta(store: Store, session_id: str, channel: str) -> None:
    """Seed an empty meta entry so a just-created session shows up before its first
    save (the ``/sessions`` list and its active marker are then immediately right)."""
    meta = store.get(_META, {}) or {}
    if session_id not in meta:
        meta[session_id] = {
            "title": "", "updated_at": _now(), "channel": channel, "msg_count": 0
        }
        store.set(_META, meta)


def new_session(store: Store, prefix: str, name: str = "", channel: str = "") -> str:
    """Create + activate a fresh session for a gateway user; returns its slug.

    A given *name* becomes the slug (slugified, de-duplicated); with none, a
    timestamped slug is used. The new session is seeded in the meta map so it
    lists immediately, and the active pointer moves to it.
    """
    base = _slugify(name) or time.strftime("s%Y%m%d-%H%M%S")
    existing = {r["slug"] for r in user_sessions(store, prefix)}
    slug = base
    i = 2
    while slug in existing:
        slug = f"{base}-{i}"
        i += 1
    store.set(_active_key(prefix), slug)
    _ensure_meta(store, _sid(prefix, slug), channel)
    return slug


def resolve_session(store: Store, prefix: str, ref: str) -> str | None:
    """Resolve a ``/resume``/``/delete`` argument to a slug: a 1-based index (as
    shown by ``/sessions``) or a literal slug. ``None`` when it matches nothing."""
    ref = (ref or "").strip()
    rows = user_sessions(store, prefix)
    if ref.isdigit():
        i = int(ref) - 1
        return rows[i]["slug"] if 0 <= i < len(rows) else None
    return next((r["slug"] for r in rows if r["slug"] == ref), None)


def set_active(store: Store, prefix: str, slug: str) -> None:
    """Point a gateway user's active session at *slug* (``/resume``)."""
    store.set(_active_key(prefix), slug)


def delete_session(store: Store, prefix: str, slug: str) -> None:
    """Delete one of a gateway user's sessions; repoint ``active`` if it was current.

    When the deleted session was active, the pointer moves to the most-recent
    remaining session, or back to a fresh ``main`` when none are left.
    """
    clear_thread(store, _sid(prefix, slug))
    if active_slug(store, prefix) == slug:
        rows = user_sessions(store, prefix)
        store.set(_active_key(prefix), rows[0]["slug"] if rows else "main")


def load_thread(store: Store, session_id: str) -> list:
    """Return the saved history for *session_id*, or ``[]`` if absent/corrupt."""
    data = store.get(_key(session_id))
    if not data:
        return []
    try:
        return list(ModelMessagesTypeAdapter.validate_python(data))
    except Exception as exc:  # noqa: BLE001 - a bad blob → start fresh, not crash
        logger.warning(
            "thread %r is unreadable (%s) — starting a fresh conversation", session_id, exc
        )
        return []


def list_threads(store: Store) -> list[str]:
    """The session ids that have a saved thread (most-recently-added last)."""
    return list(store.get(_INDEX, []) or [])


# ── Auto-titled threads (Phase 37) ───────────────────────────────────────────

def _first_user_text(messages: list) -> str:
    """The first user message's text (multimodal parts flattened to their text)."""
    for m in messages:
        for p in getattr(m, "parts", []):
            if isinstance(p, UserPromptPart):
                content = p.content
                if isinstance(content, str):
                    return content.strip()
                # A multimodal prompt: keep only the text items, drop binary parts.
                texts = [c for c in (content or []) if isinstance(c, str)]
                return " ".join(texts).strip()
    return ""


def _clean_title(text: str) -> str:
    """Normalize a raw title to a single trimmed line without wrapping quotes."""
    line = (text or "").strip()
    if not line:
        return ""
    line = line.splitlines()[0].strip().strip('"').strip("'").rstrip(".").strip()
    return line[:_TITLE_MAX_CHARS].strip()


def usage_of(result: Any) -> Any:
    """Best-effort ``RunUsage`` from an ``AgentRunResult`` (property or method form).

    A caller helper so title-call tokens can fold into the run's usage without
    every entrypoint re-deriving the property-vs-method shape (see ``_tokens`` in
    the server) — passed to :func:`autotitle_thread` as its ``usage`` accumulator.
    """
    try:
        u = result.usage
        return u if hasattr(u, "input_tokens") else u()
    except Exception:  # noqa: BLE001 - usage is optional; never break titling
        return None


async def autotitle_thread(
    store: Store,
    session_id: str,
    messages: list,
    settings: dict,
    *,
    model: Any | None = None,
    usage: Any | None = None,
) -> str | None:
    """Give *session_id* a short, human title once, if it's still untitled.

    Two tiers, chosen by ``threads.autotitle`` so cost is opt-in (Phase 37):

    - ``cheap`` (default) — a one-line title from a bounded
      ``pydantic_ai.direct.model_request`` (the same side-call pattern as
      compaction / verify), folding its tokens into *usage* when given.
    - ``off`` — the free fallback: the trimmed first user message, **zero**
      model calls.

    Called right after :func:`save_thread` (which seeds the ``threads:meta`` entry
    with an empty title). The title is **stored**, so a session pays for at most
    one small call over its whole lifetime: once a title is set this returns
    immediately without touching the model. When ``model`` is ``None`` the cheap
    tier degrades to the trimmed-first-message fallback (same posture as caching /
    MCP). A failed side-call never breaks the run — the fallback title stands.
    Returns the title written, or ``None`` when nothing was titled.
    """
    if not enabled(settings):
        return None
    mode = str((settings.get("threads") or {}).get("autotitle", "cheap")).lower()
    meta = store.get(_META, {}) or {}
    entry = dict(meta.get(session_id) or {})
    if entry.get("title"):
        return None  # already titled once — stored, never regenerated
    first = _first_user_text(messages)
    if not first:
        return None  # nothing to title on yet (no user message)

    title = _clean_title(first)  # the free fallback, also the cheap tier's backstop
    if mode != "off" and model is not None:
        try:
            response = await model_request(
                model,
                [ModelRequest(parts=[UserPromptPart(content=_TITLE_PROMPT + first[:2000])])],
                model_settings={"max_tokens": _TITLE_MAX_TOKENS},
            )
            candidate = _clean_title(
                "".join(p.content for p in response.parts if isinstance(p, TextPart))
            )
            if candidate:
                title = candidate
            if usage is not None:
                try:  # the title call costs tokens — keep Phase 3 limits honest
                    usage.incr(response.usage)
                except Exception:  # noqa: BLE001 - usage accounting must never kill a run
                    pass
        except Exception as exc:  # noqa: BLE001 - titling must never break a run
            logger.warning("could not auto-title thread %r: %s", session_id, exc)

    if not title:
        return None
    entry["title"] = title
    meta[session_id] = entry
    store.set(_META, meta)
    return title


def clear_thread(store: Store, session_id: str) -> None:
    """Delete a saved thread and drop it from the index and the metadata map."""
    store.delete(_key(session_id))
    index = store.get(_INDEX, []) or []
    if session_id in index:
        index.remove(session_id)
        store.set(_INDEX, index)
    meta = store.get(_META, {}) or {}
    if session_id in meta:
        meta.pop(session_id)
        store.set(_META, meta)
