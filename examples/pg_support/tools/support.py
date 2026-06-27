"""Drop-in tools: a support agent backed by ONE Postgres (relational + pgvector).

Demonstrates wiring a real database into a genesis vertical with **zero changes
to `agent/`**:

  - relational queries (tickets) and semantic search (a knowledge base) live in
    the SAME Postgres instance — pgvector keeps vectors in-process with the rest
    of the data, so there's no separate vector service to run;
  - the connection is a lazy, single-process singleton held in this module
    (helpers below are ``_``-prefixed, so tool discovery skips them — only the
    real tools are registered);
  - embeddings are fetched through the agent's own provider via the shared
    ``ctx.deps.http`` client (OpenAI / OpenRouter / Ollama all expose
    ``/embeddings``), so no extra SDK is pulled in.

Setup: ``docker compose up -d`` (creates the schema), ``uv sync --extra pg``,
and a ``DATABASE_URL`` in ``.env``. See this folder's README.

Note: in your OWN scaffolded agent (where you own ``agent/``) the cleaner home
for the connection is a field on ``AgentDeps`` set in ``build_deps`` — see the
README. An example can't edit the shared engine, so it uses a module singleton,
which is perfectly fine for a single-process agent (and is shared by subagents).
"""

from __future__ import annotations

import os

from pydantic_ai import RunContext

from agent import AgentDeps

# ── Connection (lazy, one per process) ───────────────────────────────────────

_conn = None


def _get_conn():
    """Return a live psycopg connection, opening it on first use."""
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn
    try:
        import psycopg
    except ImportError as exc:  # driver is opt-in (uv sync --extra pg)
        raise RuntimeError(
            "psycopg is not installed — run `uv sync --extra pg`"
        ) from exc
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    _conn = psycopg.connect(dsn, autocommit=True)
    return _conn


def _vec(embedding: list[float]) -> str:
    """Render an embedding as a pgvector literal (``[0.1,0.2,...]``)."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _embed(ctx: RunContext[AgentDeps], text: str) -> list[float]:
    """Embed *text* via the configured provider's OpenAI-compatible endpoint."""
    cfg = ctx.deps.config
    model = ctx.deps.settings.get("embed_model", "text-embedding-3-small")
    base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
    resp = ctx.deps.http.post(
        f"{base}/embeddings",
        headers={"Authorization": f"Bearer {cfg.api_key or 'not-needed'}"},
        json={"model": model, "input": text},
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


# ── Tools (auto-discovered) ──────────────────────────────────────────────────

def search_kb(ctx: RunContext[AgentDeps], query: str, limit: int = 3) -> list[dict]:
    """Semantic search the knowledge base for passages relevant to *query*.

    Embeds the query and ranks KB articles by vector similarity (pgvector
    cosine distance) — use it to ground an answer in documented facts.

    Args:
        query: What to look up, in natural language.
        limit: How many passages to return (default 3).
    """
    try:
        emb = _embed(ctx, query)
        conn = _get_conn()
        rows = conn.execute(
            "SELECT title, content, 1 - (embedding <=> %s::vector) AS score "
            "FROM kb ORDER BY embedding <=> %s::vector LIMIT %s",
            (_vec(emb), _vec(emb), int(limit)),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - surface DB/embedding errors to the model
        return [{"error": str(exc)}]
    return [{"title": t, "content": c, "score": round(s, 3)} for t, c, s in rows]


def lookup_ticket(ctx: RunContext[AgentDeps], ticket_id: int) -> dict:
    """Fetch a single support ticket by id (relational query).

    Args:
        ticket_id: The ticket's numeric id.
    """
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, customer, status, body FROM tickets WHERE id = %s",
            (int(ticket_id),),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    if row is None:
        return {"error": f"no ticket #{ticket_id}"}
    return {"id": row[0], "customer": row[1], "status": row[2], "body": row[3]}


def open_tickets(ctx: RunContext[AgentDeps], limit: int = 10) -> list[dict]:
    """List the most recent open tickets.

    Args:
        limit: Max tickets to return (default 10).
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, customer, body FROM tickets WHERE status = 'open' "
            "ORDER BY created_at DESC LIMIT %s",
            (int(limit),),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)}]
    return [{"id": i, "customer": c, "body": b} for i, c, b in rows]


def create_ticket(ctx: RunContext[AgentDeps], customer: str, body: str) -> str:
    """Open a new support ticket.

    Args:
        customer: Who is reporting the issue.
        body: The issue description.
    """
    try:
        conn = _get_conn()
        row = conn.execute(
            "INSERT INTO tickets (customer, body) VALUES (%s, %s) RETURNING id",
            (customer, body),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return f"DB error: {exc}"
    return f"Opened ticket #{row[0]} for {customer}."


def add_kb_article(ctx: RunContext[AgentDeps], title: str, content: str) -> str:
    """Add a knowledge-base article (embedded for future semantic search).

    Args:
        title: Short title.
        content: The article body.
    """
    try:
        emb = _embed(ctx, f"{title}\n\n{content}")
        conn = _get_conn()
        conn.execute(
            "INSERT INTO kb (title, content, embedding) VALUES (%s, %s, %s::vector)",
            (title, content, _vec(emb)),
        )
    except Exception as exc:  # noqa: BLE001
        return f"DB error: {exc}"
    return f"Added KB article '{title}'."
