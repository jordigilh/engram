"""Targeted seed-corpus augmentation for the ModernBERT correction-detection
spike -- adds anchors ONLY, never touches ground_truth.py's seed/eval split.

Rationale: Variant F's (label-aware k-NN) disagreement inspection surfaced a
specific residual failure pattern that survived both the embedding-model
swap (MiniLM -> ModernBERT) and the label-aware voting fix -- a short
dismissal cue ("nevermind") diluted by unrelated technical/context content
in the same message, or a message dominated by a bare URL. The existing
seed corpus's "dismissal" anchors are almost all short and bare ("nevermind",
"nevermind, carry on") with only one or two longer variants, so there's
little coverage of "dismissal + substantial unrelated elaboration" for a
nearest-neighbor search to match against.

These are hand-written, NOT paraphrases or near-duplicates of any held-out
eval example (verified by inspection) -- adding a paraphrase of an eval item
into the seed corpus would be train/test leakage and would trivially inflate
scores against that specific item without generalizing. Kept purely additive
to the existing seed pool; the held-out eval set is completely untouched.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeedOnlyExample:
    text: str
    is_correction: bool
    category: str


EXTRA_SEED_ONLY: list[SeedOnlyExample] = [
    SeedOnlyExample(
        "nevermind, that's an existing ticket already tracked in the backlog, no action needed here",
        False, "dismissal_with_context",
    ),
    SeedOnlyExample(
        "nevermind, I already applied that fix earlier today, let's move on to the next item",
        False, "dismissal_with_context",
    ),
    SeedOnlyExample(
        "nevermind, the pipeline failure was an unrelated flaky test upstream, not caused by our change",
        False, "dismissal_with_context",
    ),
    SeedOnlyExample(
        "nevermind, turns out the cache was just stale, not a real bug in the service",
        False, "dismissal_with_context",
    ),
    SeedOnlyExample(
        "https://github.com/example/repo/actions/runs/1234567/job/890123 nevermind, already resolved by the retry",
        False, "dismissal_with_context",
    ),
    SeedOnlyExample(
        "https://issues.example.com/browse/PROJ-4521 that's a separate ticket, unrelated to this PR",
        False, "dismissal_with_context",
    ),
]


def seed_extra(conn, verbose: bool = True) -> int:
    """Idempotent (checked by category, since these have a category no other
    seed row uses)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from modernbert_schema import embed_document

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM cocoindex.correction_embeddings_modernbert WHERE category = 'dismissal_with_context'"
        )
        (existing,) = cur.fetchone()
    if existing > 0:
        if verbose:
            print(f"Already augmented ({existing} rows) -- skipping.")
        return existing

    from psycopg2.extras import execute_values

    rows = []
    for ex in EXTRA_SEED_ONLY:
        emb = embed_document(ex.text)
        rows.append((ex.text, emb, "seed", ex.is_correction, ex.category, None, None))

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
        print(f"Seeded {len(rows)} targeted 'dismissal_with_context' anchors.")
    return len(rows)


if __name__ == "__main__":
    import psycopg2
    from modernbert_schema import PG_DSN

    conn = psycopg2.connect(PG_DSN)
    try:
        seed_extra(conn)
    finally:
        conn.close()
