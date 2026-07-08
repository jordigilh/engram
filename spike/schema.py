"""DDL + seeding for cocoindex.correction_embeddings (spike only).

Mirrors the existing cocoindex.code_embeddings table conventions (see
cocoindex-search.py) -- same Postgres instance, same `cocoindex` schema, same
pgvector extension already installed there. This table is spike-scoped: it is
NOT wired into the live cocoindex-flows.py pipeline.

Seeded ONLY with the "seed" split of spike/ground_truth.py -- the "eval"
split must never appear here (see ground_truth.py docstring for why).
"""
from __future__ import annotations

import os
import sys

import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(__file__))
from ground_truth import seed_examples  # noqa: E402

PG_DSN = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)

EMBEDDING_DIM = 384  # all-MiniLM-L6-v2

DDL = f"""
CREATE TABLE IF NOT EXISTS cocoindex.correction_embeddings (
    id SERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    embedding vector({EMBEDDING_DIM}) NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('seed', 'haiku_confirmed')),
    category TEXT,
    project TEXT,
    document_id TEXT,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_correction_embeddings_vec
    ON cocoindex.correction_embeddings
    USING hnsw (embedding vector_cosine_ops);
"""

_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        _embedder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _embedder


def embed_text(text: str) -> list[float]:
    model = get_embedder()
    return model.encode(text, normalize_embeddings=True).tolist()


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def reset_table(conn) -> None:
    """Drop and recreate -- spike convenience, not for production use."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cocoindex.correction_embeddings")
    conn.commit()
    ensure_schema(conn)


def seed_table(conn, verbose: bool = True) -> int:
    """Idempotent: skips seeding if the table already has seed rows."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cocoindex.correction_embeddings WHERE source = 'seed'")
        (existing,) = cur.fetchone()
    if existing > 0:
        if verbose:
            print(f"Already seeded ({existing} seed rows) -- skipping.")
        return existing

    examples = seed_examples()
    rows = []
    for ex in examples:
        emb = embed_text(ex.text)
        rows.append((ex.text, emb, "seed", ex.category, ex.project, None))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO cocoindex.correction_embeddings
                (text, embedding, source, category, project, document_id)
            VALUES %s
            """,
            rows,
            template="(%s, %s::vector, %s, %s, %s, %s)",
        )
    conn.commit()
    if verbose:
        print(f"Seeded {len(rows)} rows from ground_truth.seed_examples().")
    return len(rows)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="Drop and recreate the table first")
    args = ap.parse_args()

    conn = psycopg2.connect(PG_DSN)
    try:
        if args.reset:
            reset_table(conn)
        else:
            ensure_schema(conn)
        seed_table(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT source, count(*) FROM cocoindex.correction_embeddings GROUP BY source"
            )
            for source, count in cur.fetchall():
                print(f"  {source}: {count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
