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

from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_core import to_jsonable_python

from .store import Store

logger = logging.getLogger("agent.threads")

_PREFIX = "thread:"       # one blob per session: thread:<id>
_INDEX = "threads:index"  # list of known session ids (for /threads)


def enabled(settings: dict) -> bool:
    """True when ``threads.enabled`` is set in settings."""
    return bool((settings.get("threads") or {}).get("enabled"))


def _key(session_id: str) -> str:
    return f"{_PREFIX}{session_id}"


def save_thread(store: Store, session_id: str, messages: list, keep: int | None = None) -> None:
    """Persist *messages* (a Pydantic AI history) under *session_id*.

    *keep*, when given, trims to the last N messages so a long-running thread
    stays lean on disk (mirrors the REPL's ``history_keep`` cap). Serialization
    failures are logged and swallowed — persistence must never break a run.
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
    """Delete a saved thread and drop it from the index."""
    store.delete(_key(session_id))
    index = store.get(_INDEX, []) or []
    if session_id in index:
        index.remove(session_id)
        store.set(_INDEX, index)
