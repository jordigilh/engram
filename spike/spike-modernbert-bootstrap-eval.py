#!/usr/bin/env python3
"""Evaluate whether bootstrapping ModernBERT's training corpus from Haiku's
labels on 300 real fresh messages (modernbert_bootstrap.py) fixes the
small-sample generalization failure found by
spike-modernbert-fresh-validation.py (F1=0.94 on the 19-example hand-labeled
eval set vs. F1=0.18 on 300 fresh out-of-sample messages, using the tiny
synthetic seed set alone).

Methodology to avoid new circularity:
  1. The 300 Haiku-labeled fresh messages are split train/holdout (fixed,
     cached -- see modernbert_bootstrap.py). Only TRAIN rows get inserted
     into the DB as bootstrap training data.
  2. Variant E/F are scored on the HOLDOUT split (never used for training)
     against Haiku's own label on those messages -- same reference metric
     as the original fresh-validation run, so numbers are directly
     comparable before/after bootstrapping.
  3. As an independent sanity check NOT derived from Haiku at all, Variant
     E/F are also scored against ground_truth.eval_examples() -- the
     original 19-example HAND-labeled set (real ground truth, not a Haiku
     proxy). This catches the case where bootstrapping just makes the
     classifier agree with Haiku's blind spots rather than actually getting
     better.
  4. Both "seed-only" (baseline, pre-bootstrap) and "seed+bootstrap" runs
     are printed side by side on the exact same holdout/eval sets so the
     delta attributable to bootstrapping is unambiguous.

Run with the hindsight venv:
    ~/.hindsight/venv/bin/python3 spike/spike-modernbert-bootstrap-eval.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPIKE_DIR = REPO_ROOT / "spike"
sys.path.insert(0, str(SPIKE_DIR))

import psycopg2  # noqa: E402

import modernbert_bootstrap as bootstrap  # noqa: E402
import modernbert_schema as schema  # noqa: E402
import modernbert_variants as variants  # noqa: E402
from ground_truth import eval_examples  # noqa: E402


def hr(title: str = "") -> None:
    print()
    print("=" * 96)
    if title:
        print(title)
        print("=" * 96)


def _score(results, refs: dict[str, bool], label: str) -> None:
    tp = fp = fn = tn = 0
    for r in results:
        pred = r.predicted_correction
        actual = refs[r.text]
        if pred and actual:
            tp += 1
        elif pred and not actual:
            fp += 1
        elif not pred and actual:
            fn += 1
        else:
            tn += 1
    n = tp + fp + fn + tn
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    flagged = tp + fp
    print(
        f"  {label:32s} flagged={flagged:3d}/{n:<3d}  tp={tp:3d} fp={fp:3d} fn={fn:3d} tn={tn:3d}  "
        f"precision={precision:.2f} recall={recall:.2f} f1={f1:.2f}"
    )


def main() -> int:
    hr("Bootstrap eval: does a larger, Haiku-derived training corpus fix ModernBERT generalization?")

    conn = psycopg2.connect(schema.PG_DSN)
    schema.ensure_schema(conn)
    schema.seed_table(conn)

    split = bootstrap.get_or_create_split()
    holdout_texts = [t for t, _ in split["holdout"]]
    holdout_refs = {t: c for t, c in split["holdout"]}

    eval_ex = eval_examples()
    eval_texts = [e.text for e in eval_ex]
    eval_refs = {e.text: e.is_correction for e in eval_ex}

    hr("BEFORE bootstrap (seed-only, 19 hand-labeled examples)")
    print(f"\nHoldout (n={len(holdout_texts)}, reference = Haiku's own label on these fresh messages):")
    e_before_holdout = variants.run_variant_e_centroid(holdout_texts, conn=conn, sources=("seed",))
    _score(e_before_holdout, holdout_refs, "Variant E (centroid)")
    f_before_holdout = variants.run_variant_f_knn(holdout_texts, k=5, conn=conn)
    _score(f_before_holdout, holdout_refs, "Variant F (k=5 NN)")

    print(f"\nOriginal 19-example eval set (reference = real hand labels, independent of Haiku):")
    e_before_eval = variants.run_variant_e_centroid(eval_texts, conn=conn, sources=("seed",))
    _score(e_before_eval, eval_refs, "Variant E (centroid)")
    f_before_eval = variants.run_variant_f_knn(eval_texts, k=5, conn=conn)
    _score(f_before_eval, eval_refs, "Variant F (k=5 NN)")

    hr("Bootstrapping training corpus from Haiku labels on fresh TRAIN split...")
    bootstrap.bootstrap_train_rows(conn)

    hr("AFTER bootstrap (seed + Haiku-derived training rows)")
    print(f"\nHoldout (n={len(holdout_texts)}, reference = Haiku's own label on these fresh messages):")
    e_after_holdout = variants.run_variant_e_centroid(holdout_texts, conn=conn, sources=("seed", "haiku_confirmed"))
    _score(e_after_holdout, holdout_refs, "Variant E (centroid)")
    f_after_holdout = variants.run_variant_f_knn(holdout_texts, k=5, conn=conn)
    _score(f_after_holdout, holdout_refs, "Variant F (k=5 NN)")
    f_after_holdout15 = variants.run_variant_f_knn(holdout_texts, k=15, conn=conn)
    _score(f_after_holdout15, holdout_refs, "Variant F (k=15 NN)")

    print(f"\nOriginal 19-example eval set (reference = real hand labels, independent of Haiku):")
    e_after_eval = variants.run_variant_e_centroid(eval_texts, conn=conn, sources=("seed", "haiku_confirmed"))
    _score(e_after_eval, eval_refs, "Variant E (centroid)")
    f_after_eval = variants.run_variant_f_knn(eval_texts, k=5, conn=conn)
    _score(f_after_eval, eval_refs, "Variant F (k=5 NN)")
    f_after_eval15 = variants.run_variant_f_knn(eval_texts, k=15, conn=conn)
    _score(f_after_eval15, eval_refs, "Variant F (k=15 NN)")

    conn.close()

    hr("Done")
    print("Compare the BEFORE/AFTER blocks above. If AFTER's holdout F1 is still far below the")
    print("eval-set F1, the small-sample-bias problem is inherent to this embedding approach, not")
    print("just a training-set-size artifact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
