"""Variant C (ModernBERT-gated), Variant D (ModernBERT pure-embedding, zero
LLM), Variant E (ModernBERT nearest-centroid), and Variant F (ModernBERT
label-aware k-NN) candidate generation for the ModernBERT follow-up to the
2026-07-08 Semantic Correction Detection Spike (see docs/findings/2026-07.md).

Variant C mirrors variants.run_variant_a's architecture exactly (embed ->
cosine similarity vs. seed corpus -> Haiku validates only candidates above a
threshold), swapping in ModernBERT embeddings, so its numbers are directly
comparable to the original MiniLM Variant A row.

Variant D skips Haiku entirely -- the similarity threshold itself IS the
prediction. This isolates whether ModernBERT's embedding space is more
discriminative than MiniLM's for this task, independent of Haiku's own
(already near-perfect) classification accuracy.

Variants D's manual-inspection pass surfaced an architectural gap shared by
A/C/D: they all classify by raw similarity magnitude to the seed corpus
*without ever checking whether the matched anchor was itself a correction or
a benign near-miss* -- e.g. "nevermind, we'll have the demo scenarios team
handle it" scores 0.376 against its single nearest neighbor, which IS a
correctly-labeled NEG/dismissal anchor ("nevermind"), yet the threshold-only
design still predicts positive purely because 0.376 clears the bar. Variants
E and F fix this by actually using neighbor labels:

  - Variant E: nearest-centroid -- classify by whichever of two class
    centroids (mean embedding of all POS seed examples vs. all NEG seed
    examples) is closer. Aggregates the whole seed set into one prototype
    per class, which should be more robust to any single anchor being a
    poor match than top-1 similarity is.
  - Variant F: label-aware k-NN -- majority vote of the k nearest seed
    neighbors' true labels (not just their aggregate similarity magnitude).

Both are still zero marginal LLM cost.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from engram.classify import ClassificationResult, classify_correction  # noqa: E402
from modernbert_schema import PG_DSN, embed_query  # noqa: E402


@dataclass
class VariantResult:
    text: str
    variant: str
    was_candidate: bool
    similarity: float | None
    classification: ClassificationResult | None
    predicted_correction: bool


def _similarity_search(conn, embedding: list[float], k: int = 1) -> list[tuple[str, float]]:
    emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT text, 1 - (embedding <=> %s::vector) AS score
            FROM cocoindex.correction_embeddings_modernbert
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (emb_str, emb_str, k),
        )
        return cur.fetchall()


def run_variant_c(texts: list[str], threshold: float, conn=None) -> list[VariantResult]:
    """ModernBERT embedding-similarity gate -> Haiku validates only candidates."""
    owns_conn = conn is None
    conn = conn or psycopg2.connect(PG_DSN)
    results = []
    try:
        for text in texts:
            emb = embed_query(text)
            neighbors = _similarity_search(conn, emb, k=1)
            best_sim = neighbors[0][1] if neighbors else 0.0
            is_candidate = best_sim >= threshold
            if is_candidate:
                cls = classify_correction(text)
                predicted = cls.is_correction
            else:
                cls = None
                predicted = False
            results.append(
                VariantResult(
                    text=text, variant="C", was_candidate=is_candidate,
                    similarity=best_sim, classification=cls, predicted_correction=predicted,
                )
            )
    finally:
        if owns_conn:
            conn.close()
    return results


def run_variant_d(texts: list[str], threshold: float, conn=None) -> list[VariantResult]:
    """ModernBERT embedding-similarity classification, no LLM call at all --
    the threshold itself is the prediction."""
    owns_conn = conn is None
    conn = conn or psycopg2.connect(PG_DSN)
    results = []
    try:
        for text in texts:
            emb = embed_query(text)
            neighbors = _similarity_search(conn, emb, k=1)
            best_sim = neighbors[0][1] if neighbors else 0.0
            predicted = best_sim >= threshold
            results.append(
                VariantResult(
                    text=text, variant="D", was_candidate=predicted,
                    similarity=best_sim, classification=None, predicted_correction=predicted,
                )
            )
    finally:
        if owns_conn:
            conn.close()
    return results


def _labeled_neighbors(conn, embedding: list[float], k: int) -> list[tuple[str, bool, float]]:
    emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT text, is_correction, 1 - (embedding <=> %s::vector) AS score
            FROM cocoindex.correction_embeddings_modernbert
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (emb_str, emb_str, k),
        )
        return cur.fetchall()


def _class_centroids(conn, sources: tuple[str, ...] = ("seed",)) -> tuple[list[float], list[float]]:
    """Mean embedding of all POS rows and all NEG rows (restricted to
    `sources`), each re-normalized to unit length so cosine similarity is
    well-defined against the centroid itself.

    Defaults to the original hand-labeled "seed" rows only, matching the
    2026-08-25 spike's original behavior. Pass sources=("seed",
    "haiku_confirmed") to fold in the bootstrapped fresh-sample training
    split from modernbert_bootstrap.py -- see its module docstring for why
    the small hand-labeled seed alone was shown not to generalize
    (F1=0.94 on 19 examples vs. F1=0.18 on 300 fresh out-of-sample messages).
    """
    import numpy as np

    with conn.cursor() as cur:
        cur.execute(
            "SELECT embedding, is_correction FROM cocoindex.correction_embeddings_modernbert "
            "WHERE source = ANY(%s)",
            (list(sources),),
        )
        rows = cur.fetchall()
    pos = [np.array(_parse_vec(e)) for e, is_c in rows if is_c]
    neg = [np.array(_parse_vec(e)) for e, is_c in rows if not is_c]
    pos_centroid = np.mean(pos, axis=0)
    neg_centroid = np.mean(neg, axis=0)
    pos_centroid = pos_centroid / np.linalg.norm(pos_centroid)
    neg_centroid = neg_centroid / np.linalg.norm(neg_centroid)
    return pos_centroid.tolist(), neg_centroid.tolist()


def _parse_vec(raw) -> list[float]:
    """psycopg2 returns pgvector columns as a string like '[0.1,0.2,...]'
    unless a vector-aware type adapter is registered -- parse defensively."""
    if isinstance(raw, str):
        return [float(x) for x in raw.strip("[]").split(",")]
    return list(raw)


def run_variant_e_centroid(
    texts: list[str], conn=None, sources: tuple[str, ...] = ("seed",)
) -> list[VariantResult]:
    """Nearest-centroid classifier: closer to the mean of POS examples
    or the mean of NEG examples (drawn from `sources`)? Zero LLM cost, no
    threshold to tune -- aggregates the corpus into one prototype per class
    instead of relying on any single nearest anchor."""
    import numpy as np

    owns_conn = conn is None
    conn = conn or psycopg2.connect(PG_DSN)
    try:
        pos_centroid, neg_centroid = _class_centroids(conn, sources=sources)
        pos_c, neg_c = np.array(pos_centroid), np.array(neg_centroid)
        results = []
        for text in texts:
            emb = np.array(embed_query(text))
            sim_pos = float(np.dot(emb, pos_c))
            sim_neg = float(np.dot(emb, neg_c))
            predicted = sim_pos > sim_neg
            results.append(
                VariantResult(
                    text=text, variant="E", was_candidate=predicted,
                    similarity=sim_pos - sim_neg, classification=None,
                    predicted_correction=predicted,
                )
            )
    finally:
        if owns_conn:
            conn.close()
    return results


def run_variant_f_knn(texts: list[str], k: int, conn=None) -> list[VariantResult]:
    """Label-aware k-NN: majority vote of the k nearest seed anchors' TRUE
    labels, not just aggregate similarity magnitude. Ties (only possible for
    even k) break toward "not a correction" -- false negatives are cheaper
    than false positives here, matching classify.py's own tie-break policy."""
    owns_conn = conn is None
    conn = conn or psycopg2.connect(PG_DSN)
    results = []
    try:
        for text in texts:
            emb = embed_query(text)
            neighbors = _labeled_neighbors(conn, emb, k=k)
            votes = sum(1 for _, is_c, _ in neighbors if is_c)
            predicted = votes > (k / 2)
            avg_sim = sum(s for _, _, s in neighbors) / len(neighbors) if neighbors else 0.0
            results.append(
                VariantResult(
                    text=text, variant="F", was_candidate=predicted,
                    similarity=avg_sim, classification=None, predicted_correction=predicted,
                )
            )
    finally:
        if owns_conn:
            conn.close()
    return results
