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
from datetime import datetime, timezone

from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_core import to_jsonable_python

from .store import Store

logger = logging.getLogger("agent.threads")

_PREFIX = "thread:"       # one blob per session: thread:<id>
_INDEX = "threads:index"  # list of known session ids (for /threads)
_META = "threads:meta"    # {id: {title, updated_at, channel, msg_count}} (Phase 36)


def enabled(settings: dict) -> bool:
    """True when ``threads.enabled`` is set in settings."""
    return bool((settings.get("threads") or {}).get("enabled"))


def _key(session_id: str) -> str:
    return f"{_PREFIX}{session_id}"


def _now() -> str:
    """UTC timestamp for ``updated_at`` (second precision, ISO 8601)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
