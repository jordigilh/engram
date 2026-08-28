"""Bootstrap a much larger ModernBERT training corpus from Haiku's own
labels on the 300-message fresh out-of-sample set (mine_fresh_messages.py +
spike-modernbert-fresh-validation.py), instead of the tiny 19-example
hand-labeled seed set alone.

Why: spike-modernbert-fresh-validation.py showed the centroid/kNN
classifiers trained on the small synthetic seed set do NOT generalize --
F1=0.94 on the 19-example eval set collapsed to F1=0.18 on 300 real,
diverse messages (precision=0.10 -- it flagged 194/300 messages as
corrections when Haiku only flagged 25/300). That result was itself
produced by Haiku (Variant B), whose own accuracy against real hand-labeled
ground truth was already measured at F1=0.97 in the original 2026-07-08
spike -- so treating Haiku's labels on this larger, more diverse corpus as
a (imperfect but far larger and more representative) training signal is a
reasonable way to test whether the failure was "small/unrepresentative
training set" rather than "ModernBERT's embedding space is fundamentally
unsuited to this task".

This does NOT call Haiku again -- it reads the already-cached labels from
spike-modernbert-fresh-validation.py's HAIKU_CACHE_PATH (persisted so we
"don't have to run them again next time", per 2026-08-25 direction). If
that cache doesn't have all 300 fresh messages labeled yet, run
spike-modernbert-fresh-validation.py first.

Train/holdout split: stratified by is_correction, fixed seed, cached to
disk so re-running this module (e.g. after a classifier tweak) always
scores against the SAME held-out messages -- otherwise "generalizes to
holdout" could quietly become "overfits to whatever holdout we happened to
draw this run", the exact circularity this whole exercise exists to avoid.
"""
from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))
from mine_fresh_messages import CACHE_DIR, CACHE_PATH  # noqa: E402
from modernbert_schema import PG_DSN, embed_document  # noqa: E402

import psycopg2  # noqa: E402
from psycopg2.extras import execute_values  # noqa: E402

HAIKU_CACHE_PATH = os.path.join(CACHE_DIR, "fresh_haiku_labels.json")
SPLIT_CACHE_PATH = os.path.join(CACHE_DIR, "bootstrap_split.json")

HOLDOUT_FRACTION = 0.25
SPLIT_SEED = 13


def _load_fresh_with_labels() -> list[tuple[str, bool]]:
    with open(CACHE_PATH) as f:
        sample = json.load(f)
    with open(HAIKU_CACHE_PATH) as f:
        haiku = json.load(f)
    missing = [t for t in sample if t not in haiku]
    if missing:
        raise RuntimeError(
            f"{len(missing)}/{len(sample)} fresh messages have no cached Haiku label yet -- "
            "run spike-modernbert-fresh-validation.py first so its Haiku cache covers the full sample."
        )
    return [(t, haiku[t]["is_correction"]) for t in sample]


def get_or_create_split() -> dict:
    """Returns {"train": [[text, is_correction], ...], "holdout": [...]}.
    Cached to disk so the holdout set is stable across runs."""
    if os.path.exists(SPLIT_CACHE_PATH):
        with open(SPLIT_CACHE_PATH) as f:
            return json.load(f)

    labeled = _load_fresh_with_labels()
    pos = [(t, c) for t, c in labeled if c]
    neg = [(t, c) for t, c in labeled if not c]

    rng = random.Random(SPLIT_SEED)
    rng.shuffle(pos)
    rng.shuffle(neg)

    def split(group: list) -> tuple[list, list]:
        n_holdout = max(1, round(len(group) * HOLDOUT_FRACTION))
        return group[n_holdout:], group[:n_holdout]  # train, holdout

    pos_train, pos_holdout = split(pos)
    neg_train, neg_holdout = split(neg)

    result = {
        "train": [[t, c] for t, c in pos_train + neg_train],
        "holdout": [[t, c] for t, c in pos_holdout + neg_holdout],
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SPLIT_CACHE_PATH, "w") as f:
        json.dump(result, f)
    print(
        f"Split {len(labeled)} Haiku-labeled fresh messages: "
        f"train={len(result['train'])} ({len(pos_train)} pos / {len(neg_train)} neg), "
        f"holdout={len(result['holdout'])} ({len(pos_holdout)} pos / {len(neg_holdout)} neg)"
    )
    return result


def bootstrap_train_rows(conn, verbose: bool = True) -> int:
    """Idempotent: clears any previously-inserted haiku_confirmed rows from
    this bootstrap (identified by document_id='fresh_bootstrap') and
    re-inserts the current train split, so re-running after a split-cache
    reset doesn't accumulate duplicates."""
    split = get_or_create_split()
    train = split["train"]

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM cocoindex.correction_embeddings_modernbert "
            "WHERE source = 'haiku_confirmed' AND document_id = 'fresh_bootstrap'"
        )
    conn.commit()

    rows = []
    for text, is_correction in train:
        emb = embed_document(text)
        rows.append((text, emb, "haiku_confirmed", is_correction, None, None, "fresh_bootstrap"))

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
        n_pos = sum(1 for _, c in train if c)
        print(f"Bootstrapped {len(rows)} haiku_confirmed rows ({n_pos} pos / {len(rows) - n_pos} neg).")
    return len(rows)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--reset-split", action="store_true", help="Discard the cached train/holdout split and redraw it")
    args = ap.parse_args()

    if args.reset_split and os.path.exists(SPLIT_CACHE_PATH):
        os.remove(SPLIT_CACHE_PATH)

    conn = psycopg2.connect(PG_DSN)
    try:
        bootstrap_train_rows(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
