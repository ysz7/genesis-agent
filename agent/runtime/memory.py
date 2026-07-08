"""Phase 19: lightweight semantic long-term memory (opt-in).

Phase 11f recalls the *most recent* lessons; this recalls the most *relevant*
ones to the current task — **without a vector database**. When `memory.semantic`
is on, `remember` also stores an embedding alongside each lesson, and recall
ranks by cosine similarity to the current query. Embeddings come from the
configured provider's OpenAI-compatible `/embeddings` endpoint via the shared
http client; similarity is a few lines of pure Python. Any failure (no endpoint,
timeout, a non-embeddings provider) degrades to recency — it never blocks or
crashes a run.

This is *lightweight relevance recall*, NOT a RAG engine: it ranks the short
lessons the agent wrote, capped to `memory.max_vectors` entries on disk.
"""

from __future__ import annotations

import json
import logging
import math

logger = logging.getLogger("agent.memory")

DEFAULT_EMBED_MODEL = "text-embedding-3-small"
DEFAULT_MAX_VECTORS = 500


def semantic_enabled(settings: dict) -> bool:
    """True when ``memory.semantic`` is set."""
    return bool((settings.get("memory") or {}).get("semantic"))


def _opt(settings: dict, key: str, default):
    return (settings.get("memory") or {}).get(key, default)


def embed(deps, text: str, timeout: float = 10.0) -> list[float] | None:
    """Embed *text* via the provider's ``/embeddings`` endpoint; None on failure.

    Provider-agnostic (any OpenAI-compatible endpoint: OpenAI · OpenRouter ·
    Ollama). A provider without an embeddings endpoint (e.g. Anthropic) simply
    returns None, and the caller falls back to recency.
    """
    cfg = deps.config
    model = _opt(deps.settings, "embed_model", DEFAULT_EMBED_MODEL)
    base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
    try:
        resp = deps.http.post(
            f"{base}/embeddings",
            headers={"Authorization": f"Bearer {cfg.api_key or 'not-needed'}"},
            json={"model": model, "input": text},
            timeout=timeout,
        )
        resp.raise_for_status()
        vec = resp.json()["data"][0]["embedding"]
        return [float(x) for x in vec] if vec else None
    except Exception as exc:  # noqa: BLE001 - recall must never break a run
        logger.debug("embedding failed (%s) — falling back to recency", exc)
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


def current_query(ctx) -> str:
    """Best-effort current user prompt from a RunContext (for relevance ranking)."""
    p = getattr(ctx, "prompt", None)
    if isinstance(p, str):
        return p
    if isinstance(p, (list, tuple)):
        return " ".join(x for x in p if isinstance(x, str))
    for msg in reversed(getattr(ctx, "messages", []) or []):
        for part in getattr(msg, "parts", []):
            if type(part).__name__ == "UserPromptPart":
                content = getattr(part, "content", "")
                if isinstance(content, str):
                    return content
    return ""


def _load(path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def trim(path, max_vectors: int) -> None:
    """Keep only the last *max_vectors* lines of *path* (lean on disk)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > max_vectors:
        path.write_text("\n".join(lines[-max_vectors:]) + "\n", encoding="utf-8")


def semantic_recall(deps, query: str, limit: int) -> str:
    """A relevance-ranked lessons digest for the system prompt.

    Ranks stored lessons by cosine similarity to *query*. Falls back to recency
    (the last *limit*) when there are no stored embeddings or the query can't be
    embedded — so it degrades to exactly Phase 11f's behaviour, never failing.
    """
    path = deps.memory_dir / "lessons.jsonl"
    entries = _load(path)
    if not entries:
        return ""

    have_vectors = any(e.get("embedding") for e in entries)
    qvec = embed(deps, query) if (have_vectors and query.strip()) else None

    if qvec is None:                                   # recency fallback
        chosen = entries[-limit:]
    else:
        chosen = sorted(
            entries, key=lambda e: _cosine(qvec, e.get("embedding") or []), reverse=True
        )[:limit]

    lessons = [str(e.get("lesson", "")).strip() for e in chosen]
    lessons = [lesson for lesson in lessons if lesson]
    if not lessons:
        return ""
    header = "Relevant lessons from past sessions:" if qvec is not None \
        else "Lessons from past sessions:"
    return header + "\n" + "\n".join(f"- {lesson}" for lesson in lessons)
