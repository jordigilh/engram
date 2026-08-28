"""DDL + seeding for cocoindex.correction_embeddings_modernbert (spike only).

Sibling of schema.py, swapping the embedding model from
sentence-transformers/all-MiniLM-L6-v2 (384-dim) to nomic-ai/modernbert-embed-base
(768-dim) to test whether a stronger encoder changes the 2026-07-08 spike's
conclusion that an embedding-similarity gate underperforms regex/direct-Haiku
for correction detection (see docs/findings/2026-07.md). Separate table (not
just a re-embed of the same one) because the vector dimension differs and
because keeping both tables lets the original MiniLM variant be re-run
side-by-side in the same session for a directly comparable number.

modernbert-embed-base is trained like Nomic Embed and REQUIRES asymmetric
prefixes ("search_query: " for the text being classified, "search_document: "
for the seed corpus) -- omitting them measurably degrades retrieval quality
per the model card. This table is spike-scoped: it is NOT wired into the live
cocoindex-flows.py pipeline.

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

EMBEDDING_MODEL = "nomic-ai/modernbert-embed-base"
EMBEDDING_DIM = 768

DDL = f"""
CREATE TABLE IF NOT EXISTS cocoindex.correction_embeddings_modernbert (
    id SERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    embedding vector({EMBEDDING_DIM}) NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('seed', 'haiku_confirmed')),
    is_correction BOOLEAN NOT NULL,
    category TEXT,
    project TEXT,
    document_id TEXT,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_correction_embeddings_modernbert_vec
    ON cocoindex.correction_embeddings_modernbert
    USING hnsw (embedding vector_cosine_ops);
"""

_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        _embedder = SentenceTransformer(EMBEDDING_MODEL)
    return _embedder


def embed_query(text: str) -> list[float]:
    """Embed a candidate message being classified ("is this a correction")."""
    model = get_embedder()
    return model.encode(f"search_query: {text}", normalize_embeddings=True).tolist()


def embed_document(text: str) -> list[float]:
    """Embed a seed-corpus example (the anchor corpus being searched against)."""
    model = get_embedder()
    return model.encode(f"search_document: {text}", normalize_embeddings=True).tolist()


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def reset_table(conn) -> None:
    """Drop and recreate -- spike convenience, not for production use."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cocoindex.correction_embeddings_modernbert")
    conn.commit()
    ensure_schema(conn)


def seed_table(conn, verbose: bool = True) -> int:
    """Idempotent: skips seeding if the table already has seed rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM cocoindex.correction_embeddings_modernbert WHERE source = 'seed'"
        )
        (existing,) = cur.fetchone()
    if existing > 0:
        if verbose:
            print(f"Already seeded ({existing} seed rows) -- skipping.")
        return existing

    examples = seed_examples()
    rows = []
    for ex in examples:
        emb = embed_document(ex.text)
        rows.append((ex.text, emb, "seed", ex.is_correction, ex.category, ex.project, None))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO cocoindex.correction_embeddings_modernbert
                (text, embedding, source, is_correction, category, project, document_id)
            VALUES %s
            """,
            rows,
            template="(%s, %s::vector, %s, %s, %s, %s, %s)",
        )
    conn.commit()
    if verbose:
        print(f"Seeded {len(rows)} rows from ground_truth.seed_examples() (ModernBERT embeddings).")
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
                "SELECT source, count(*) FROM cocoindex.correction_embeddings_modernbert GROUP BY source"
            )
            for source, count in cur.fetchall():
                print(f"  {source}: {count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
