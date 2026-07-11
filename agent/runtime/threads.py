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
