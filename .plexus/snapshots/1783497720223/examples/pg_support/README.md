# Example vertical — Postgres + pgvector support agent

A support agent backed by **one Postgres instance** that holds *both* the
relational data (tickets) **and** the vector knowledge base (pgvector). Keeping
vectors in the same database means **no separate vector service** to run — one
process, one connection, one thing to back up.

Built with **zero changes to `agent/`** — it's only `persona.md`,
`settings.yaml`, and `tools/`.

| Seam | Here |
|------|------|
| Drop-in tools | [`tools/support.py`](tools/support.py) — `search_kb`, `lookup_ticket`, `open_tickets`, `create_ticket`, `add_kb_article` |
| Relational + vector in one DB | [`schema.sql`](schema.sql) — `tickets` table + `kb` table with a `vector(1536)` column |
| Embeddings | via the agent's own provider (`/embeddings`) through `ctx.deps.http` — no extra SDK |
| DB connection | a lazy, single-process singleton in `support.py` (helpers are `_`-prefixed, so discovery skips them) |
| `settings.yaml` | `embed_model` (must match the schema's vector dimension) |

## Run it

```bash
# 1. start one Postgres with pgvector (schema is applied automatically)
cd examples/pg_support
docker compose up -d

# 2. install the Postgres driver (opt-in extra) + set your keys
uv sync --extra pg
cp .env.example .env        # set PROVIDER/MODEL/API_KEY (or use Ollama) + DATABASE_URL

# 3. seed a little knowledge, then ask
uv run agent "Add a KB article titled 'Resetting your password' explaining users click Settings > Security > Reset."
uv run agent "How do I reset my password?"     # → grounded in the KB via vector search
uv run agent "Open a ticket for Acme: export to CSV fails on large reports."
uv run agent "What tickets are open?"
```

(Run from this folder so `.env` / `settings.yaml` / `tools/` are picked up; from
elsewhere add `--root examples/pg_support`.)

## How the database is wired (and the cleaner option for your own agent)

This example can't edit the shared `agent/` engine, so it keeps the connection
as a **module-level singleton** in `tools/support.py` — lazy-opened on first use,
reused across turns, and (since subagents share `deps`, and this lives in the
module) shared process-wide. That's perfectly fine for a single-process agent.

In **your own** scaffolded agent (where you own a copy of `agent/`), the tidier
home is the DI bundle — add one field to `AgentDeps` and open it in `build_deps`,
then every tool reaches it via `ctx.deps.db` with no per-tool setup:

```python
# agent/runtime/context.py
@dataclass
class AgentDeps:
    ...
    db: object | None = None          # your psycopg connection / pool

def build_deps(config):
    ...
    db = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    return AgentDeps(..., db=db)

def close_deps(deps):
    try: deps.http.close()
    finally:
        deps.store.close()
        if deps.db is not None: deps.db.close()
```

Both patterns are "wire it once, every tool sees it." Pick the singleton for a
quick add or a shared example; pick the `AgentDeps` field when you own the engine
and want lifecycle management (open/close) handled in one place.

## Notes

- **Embedding dimension must match the model.** Default is `text-embedding-3-small`
  (1536). For Ollama's `nomic-embed-text` (768), change `vector(1536)` in
  `schema.sql` and `embed_model` in `settings.yaml`.
- The genesis `store` (`workspace/state.json`) is still available for small
  cross-run state; the domain data deliberately lives in Postgres instead.
- Tools return error strings (never raise) on a DB/embedding failure, so a
  down database degrades gracefully instead of crashing the run.
