-- Schema for the pg_support example: relational + vector in ONE Postgres.
-- Auto-applied by docker-compose on first start (mounted into
-- /docker-entrypoint-initdb.d/). Run by hand with:  psql "$DATABASE_URL" -f schema.sql

CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector: vectors live in this same DB

CREATE TABLE IF NOT EXISTS tickets (
    id          SERIAL PRIMARY KEY,
    customer    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- embedding dimension must match your embed_model:
--   text-embedding-3-small / -ada-002 → 1536   (the default here)
--   nomic-embed-text (Ollama)         → 768     (change vector(1536) below)
CREATE TABLE IF NOT EXISTS kb (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(1536)
);

-- Approximate-nearest-neighbour index for fast cosine search (optional but
-- recommended once the table grows).
CREATE INDEX IF NOT EXISTS kb_embedding_idx
    ON kb USING hnsw (embedding vector_cosine_ops);
