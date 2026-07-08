"""Variant A (embedding-gated) and Variant B (classify-everything) candidate
generation + classification, sharing the same Haiku classify_correction call
(see classify.py). Both variants are evaluated against the same held-out
data in spike_run.py so their precision/recall/cost can be compared directly.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg2

from classify import ClassificationResult, classify_correction
from schema import PG_DSN, embed_text


@dataclass
class VariantResult:
    text: str
    variant: str
    was_candidate: bool  # False only possible for Variant A (didn't pass gate)
    similarity: float | None
    classification: ClassificationResult | None
    predicted_correction: bool


def _similarity_search(conn, embedding: list[float], k: int = 3) -> list[tuple[str, float]]:
    emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT text, 1 - (embedding <=> %s::vector) AS score
            FROM cocoindex.correction_embeddings
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (emb_str, emb_str, k),
        )
        return cur.fetchall()


def run_variant_a(texts: list[str], threshold: float, conn=None) -> list[VariantResult]:
    """Embedding-similarity gate -> Haiku validates only messages that pass."""
    owns_conn = conn is None
    conn = conn or psycopg2.connect(PG_DSN)
    results = []
    try:
        for text in texts:
            emb = embed_text(text)
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
                    text=text, variant="A", was_candidate=is_candidate,
                    similarity=best_sim, classification=cls, predicted_correction=predicted,
                )
            )
    finally:
        if owns_conn:
            conn.close()
    return results


def run_variant_b(texts: list[str]) -> list[VariantResult]:
    """No gate -- Haiku classifies every message directly."""
    results = []
    for text in texts:
        cls = classify_correction(text)
        results.append(
            VariantResult(
                text=text, variant="B", was_candidate=True,
                similarity=None, classification=cls, predicted_correction=cls.is_correction,
            )
        )
    return results
