#!/usr/bin/env python3
"""ModernBERT Correction Detection Follow-up Spike -- evaluation harness.

Follow-up to the 2026-07-08 Semantic Correction Detection Spike (see
docs/findings/2026-07.md), which found that an embedding-similarity gate
(MiniLM cosine similarity vs. a seed corpus, then Haiku validates only
candidates above a threshold -- "Variant A") underperformed both regex and
direct Haiku classification ("Variant B") at every threshold tested, because
short/stylistically-varied correction phrasing doesn't cluster reliably
against a small seed corpus in MiniLM's embedding space.

This spike asks: does swapping in a stronger encoder (nomic-ai/
modernbert-embed-base, 768-dim, trained on modern retrieval objectives)
change that conclusion? Two new variants, both reusing the exact same
held-out ground truth and scoring so results are directly comparable to the
original table:

  - Variant C: same gate architecture as the original Variant A, ModernBERT
    embeddings instead of MiniLM (directly comparable row).
  - Variant D: ModernBERT embedding-similarity used AS the classifier itself,
    no Haiku call at all -- isolates whether ModernBERT's embedding space is
    more discriminative than MiniLM's, independent of Haiku's own accuracy.
    Zero marginal LLM cost if it ever clears the bar.

Original Variant A (MiniLM) and Variant B (Haiku, no gate) are re-run fresh
in this same session (not hardcoded from the prior write-up) for a clean,
directly comparable baseline.

Run with the hindsight venv (has litellm/vertexai/sentence-transformers/
psycopg2/transformers installed):
    ~/.hindsight/venv/bin/python3 spike/spike-modernbert-correction-detection.py

Nothing here is wired into production -- same ground rules as the original
spike.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPIKE_DIR = REPO_ROOT / "spike"
sys.path.insert(0, str(SPIKE_DIR))

from ground_truth import DATASET, eval_examples, seed_examples  # noqa: E402
import modernbert_schema  # noqa: E402
import modernbert_seed_augment  # noqa: E402
import modernbert_variants  # noqa: E402
import schema  # noqa: E402
import variants  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "src"))
from engram.pipeline import nightly_learn as nl  # noqa: E402


def hr(title: str = "") -> None:
    print()
    print("=" * 96)
    if title:
        print(title)
        print("=" * 96)


def step0_preflight() -> bool:
    hr("STEP 0: Preflight -- ModernBERT embedder + Haiku smoke test")
    import importlib.util

    result = importlib.util.spec_from_file_location(
        "preflight", SPIKE_DIR / "preflight_smoke_test.py"
    )
    mod = importlib.util.module_from_spec(result)
    result.loader.exec_module(mod)
    haiku_ok = mod.main() == 0
    if not haiku_ok:
        print("\nABORTING: Haiku preflight failed. Fix auth before continuing.")
        return False

    t0 = time.time()
    emb = modernbert_schema.embed_query("smoke test")
    elapsed = time.time() - t0
    if len(emb) != modernbert_schema.EMBEDDING_DIM:
        print(f"ABORTING: expected {modernbert_schema.EMBEDDING_DIM}-dim embedding, got {len(emb)}")
        return False
    print(f"ModernBERT embedder OK: {len(emb)}-dim, {elapsed:.2f}s (includes cold load if first call)")
    return True


def step1_seed_tables() -> None:
    hr("STEP 1: Ensure both embedding tables are seeded (MiniLM existing, ModernBERT new)")
    import psycopg2

    conn = psycopg2.connect(schema.PG_DSN)
    try:
        schema.ensure_schema(conn)
        schema.seed_table(conn)
    finally:
        conn.close()

    conn = psycopg2.connect(modernbert_schema.PG_DSN)
    try:
        modernbert_schema.ensure_schema(conn)
        modernbert_schema.seed_table(conn)
        # Targeted augmentation (see modernbert_seed_augment.py docstring): a
        # handful of hand-written anchors for a specific pattern the
        # unaugmented seed corpus under-covered ("dismissal" cue diluted by
        # unrelated technical elaboration, or a bare-URL-dominated message).
        # Additive only -- the held-out eval set is never touched.
        modernbert_seed_augment.seed_extra(conn)
    finally:
        conn.close()


def score(predictions: dict[str, bool], truth: dict[str, bool]) -> dict:
    tp = sum(1 for k in truth if truth[k] and predictions.get(k, False))
    fp = sum(1 for k in truth if not truth[k] and predictions.get(k, False))
    fn = sum(1 for k in truth if truth[k] and not predictions.get(k, False))
    tn = sum(1 for k in truth if not truth[k] and not predictions.get(k, False))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall, "f1": f1}


def _print_row(label: str, s: dict, calls, elapsed) -> None:
    print(f"{label:<34} {s['precision']:>6.2f} {s['recall']:>6.2f} {s['f1']:>6.2f} "
          f"{s['tp']:>4} {s['fp']:>4} {s['fn']:>4} {calls!s:>10} {elapsed:>7.1f}s")


def step2_compare() -> dict:
    hr("STEP 2: Regex vs. Variant A (MiniLM) vs. Variant B (Haiku-only) vs. "
       "Variant C (ModernBERT-gated) vs. Variant D (ModernBERT-only, zero LLM)")
    eval_set = eval_examples()
    truth = {ex.text: ex.is_correction for ex in eval_set}
    texts = [ex.text for ex in eval_set]
    print(f"Held-out eval set: {len(eval_set)} examples "
          f"({sum(truth.values())} corrections, {len(truth) - sum(truth.values())} benign)")
    print("(Same held-out set as the original 2026-07-08 spike -- never used to seed either")
    print("embedding table or as a few-shot example.)\n")

    thresholds = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55]
    results: dict = {"thresholds": thresholds}

    # --- Regex baseline (free) ---
    regex_preds = {t: nl.is_correction(t) for t in texts}
    regex_score = score(regex_preds, truth)
    results["regex"] = regex_score

    # --- Variant B: Haiku classifies every message, no gate (re-run fresh) ---
    print("Running Variant B (Haiku, no gate)...")
    t0 = time.time()
    b_results = variants.run_variant_b(texts)
    b_elapsed = time.time() - t0
    b_preds = {r.text: r.predicted_correction for r in b_results}
    b_score = score(b_preds, truth)
    b_calls = sum(1 for r in b_results if r.classification is not None)
    results["variant_b"] = {"score": b_score, "calls": b_calls, "elapsed": b_elapsed}

    # --- Variant A: MiniLM gate, re-run fresh across the same threshold sweep ---
    print("Running Variant A (MiniLM-gated) across threshold sweep...")
    import psycopg2

    conn = psycopg2.connect(schema.PG_DSN)
    a_sweep = {}
    for th in thresholds:
        t0 = time.time()
        a_results = variants.run_variant_a(texts, threshold=th, conn=conn)
        elapsed = time.time() - t0
        a_preds = {r.text: r.predicted_correction for r in a_results}
        a_sweep[th] = {"score": score(a_preds, truth), "elapsed": elapsed,
                        "n_candidates": sum(1 for r in a_results if r.was_candidate),
                        "results": a_results}
    conn.close()
    results["variant_a_sweep"] = a_sweep

    # --- Variant C: ModernBERT gate, same threshold sweep ---
    print("Running Variant C (ModernBERT-gated) across threshold sweep...")
    conn = psycopg2.connect(modernbert_schema.PG_DSN)
    c_sweep = {}
    for th in thresholds:
        t0 = time.time()
        c_results = modernbert_variants.run_variant_c(texts, threshold=th, conn=conn)
        elapsed = time.time() - t0
        c_preds = {r.text: r.predicted_correction for r in c_results}
        c_sweep[th] = {"score": score(c_preds, truth), "elapsed": elapsed,
                        "n_candidates": sum(1 for r in c_results if r.was_candidate),
                        "results": c_results}
    results["variant_c_sweep"] = c_sweep

    # --- Variant D: ModernBERT-only classification, zero LLM calls ---
    print("Running Variant D (ModernBERT-only, zero LLM) across threshold sweep...")
    d_sweep = {}
    for th in thresholds:
        t0 = time.time()
        d_results = modernbert_variants.run_variant_d(texts, threshold=th, conn=conn)
        elapsed = time.time() - t0
        d_preds = {r.text: r.predicted_correction for r in d_results}
        d_sweep[th] = {"score": score(d_preds, truth), "elapsed": elapsed,
                        "n_candidates": sum(1 for r in d_results if r.was_candidate),
                        "results": d_results}
    results["variant_d_sweep"] = d_sweep

    # --- Variant E: nearest-centroid, zero LLM calls, no threshold to tune ---
    print("Running Variant E (ModernBERT nearest-centroid, zero LLM)...")
    t0 = time.time()
    e_results = modernbert_variants.run_variant_e_centroid(texts, conn=conn)
    e_elapsed = time.time() - t0
    e_preds = {r.text: r.predicted_correction for r in e_results}
    e_score = score(e_preds, truth)
    results["variant_e"] = {"score": e_score, "elapsed": e_elapsed, "results": e_results}

    # --- Variant F: label-aware k-NN, zero LLM calls ---
    print("Running Variant F (ModernBERT label-aware k-NN) across k sweep...")
    f_sweep = {}
    for k in (1, 3, 5, 7):
        t0 = time.time()
        f_results = modernbert_variants.run_variant_f_knn(texts, k=k, conn=conn)
        elapsed = time.time() - t0
        f_preds = {r.text: r.predicted_correction for r in f_results}
        f_sweep[k] = {"score": score(f_preds, truth), "elapsed": elapsed, "results": f_results}
    results["variant_f_sweep"] = f_sweep
    conn.close()

    best_a = max(thresholds, key=lambda t: a_sweep[t]["score"]["f1"])
    best_c = max(thresholds, key=lambda t: c_sweep[t]["score"]["f1"])
    best_d = max(thresholds, key=lambda t: d_sweep[t]["score"]["f1"])
    best_f = max(f_sweep, key=lambda k: f_sweep[k]["score"]["f1"])
    results["best_thresholds"] = {"a": best_a, "c": best_c, "d": best_d, "f": best_f}

    print(f"\n{'Method':<34} {'Prec':>6} {'Rec':>6} {'F1':>6} {'TP':>4} {'FP':>4} {'FN':>4} {'LLM calls':>10} {'Time':>8}")
    print("-" * 108)
    _print_row("Regex (production)", regex_score, "0 (free)", 0.0)
    _print_row("Variant B (Haiku, no gate)", b_score, b_calls, b_elapsed)
    for th in thresholds:
        s = a_sweep[th]["score"]
        marker = " *best F1*" if th == best_a else ""
        _print_row(f"Variant A (MiniLM, t={th:.2f})" + marker, s, a_sweep[th]["n_candidates"], a_sweep[th]["elapsed"])
    for th in thresholds:
        s = c_sweep[th]["score"]
        marker = " *best F1*" if th == best_c else ""
        _print_row(f"Variant C (ModernBERT+Haiku, t={th:.2f})" + marker, s, c_sweep[th]["n_candidates"], c_sweep[th]["elapsed"])
    for th in thresholds:
        s = d_sweep[th]["score"]
        marker = " *best F1*" if th == best_d else ""
        _print_row(f"Variant D (ModernBERT-only, t={th:.2f})" + marker, s, "0 (free)", d_sweep[th]["elapsed"])
    _print_row("Variant E (ModernBERT centroid)", e_score, "0 (free)", e_elapsed)
    for k in (1, 3, 5, 7):
        s = f_sweep[k]["score"]
        marker = " *best F1*" if k == best_f else ""
        _print_row(f"Variant F (ModernBERT kNN, k={k})" + marker, s, "0 (free)", f_sweep[k]["elapsed"])

    return results


def step3_inspect_disagreements(results: dict) -> None:
    hr("STEP 3: Manual-inspection disagreements (mandatory for heuristic code/text "
       "classifiers -- aggregate F1 alone is not sufficient evidence)")
    eval_set = eval_examples()
    truth = {ex.text: ex.is_correction for ex in eval_set}

    best_c_th = results["best_thresholds"]["c"]
    best_d_th = results["best_thresholds"]["d"]
    c_results = results["variant_c_sweep"][best_c_th]["results"]
    d_results = results["variant_d_sweep"][best_d_th]["results"]

    print(f"\nVariant C (ModernBERT-gated, threshold={best_c_th}) vs. ground truth:")
    any_c_mismatch = False
    for r in c_results:
        if r.predicted_correction != truth[r.text]:
            any_c_mismatch = True
            print(f"  pred={r.predicted_correction!s:5s} truth={truth[r.text]!s:5s} "
                  f"sim={r.similarity:.3f} candidate={r.was_candidate!s:5s}  {r.text[:80]}")
    if not any_c_mismatch:
        print("  (none -- Variant C matched ground truth on every eval example at this threshold)")

    print(f"\nVariant D (ModernBERT-only, threshold={best_d_th}) vs. ground truth:")
    any_d_mismatch = False
    for r in d_results:
        if r.predicted_correction != truth[r.text]:
            any_d_mismatch = True
            print(f"  pred={r.predicted_correction!s:5s} truth={truth[r.text]!s:5s} "
                  f"sim={r.similarity:.3f}  {r.text[:80]}")
    if not any_d_mismatch:
        print("  (none -- Variant D matched ground truth on every eval example at this threshold)")

    print("\nVariant E (ModernBERT nearest-centroid) vs. ground truth:")
    any_e_mismatch = False
    for r in results["variant_e"]["results"]:
        if r.predicted_correction != truth[r.text]:
            any_e_mismatch = True
            print(f"  pred={r.predicted_correction!s:5s} truth={truth[r.text]!s:5s} "
                  f"margin={r.similarity:+.3f}  {r.text[:80]}")
    if not any_e_mismatch:
        print("  (none -- Variant E matched ground truth on every eval example)")

    best_f_k = results["best_thresholds"]["f"]
    print(f"\nVariant F (ModernBERT label-aware k-NN, k={best_f_k}) vs. ground truth:")
    any_f_mismatch = False
    for r in results["variant_f_sweep"][best_f_k]["results"]:
        if r.predicted_correction != truth[r.text]:
            any_f_mismatch = True
            print(f"  pred={r.predicted_correction!s:5s} truth={truth[r.text]!s:5s} "
                  f"avg_sim={r.similarity:.3f}  {r.text[:80]}")
    if not any_f_mismatch:
        print(f"  (none -- Variant F (k={best_f_k}) matched ground truth on every eval example)")


def main() -> int:
    if not step0_preflight():
        return 1
    step1_seed_tables()
    results = step2_compare()
    step3_inspect_disagreements(results)

    hr("SUMMARY")
    print(f"Seed examples: {len(seed_examples())}  Held-out eval examples: {len(eval_examples())}  "
          f"Total labeled: {len(DATASET)}")
    print(f"\nRegex (production):        precision={results['regex']['precision']:.2f}  "
          f"recall={results['regex']['recall']:.2f}  f1={results['regex']['f1']:.2f}")
    vb = results["variant_b"]["score"]
    print(f"Variant B (Haiku only):    precision={vb['precision']:.2f}  recall={vb['recall']:.2f}  f1={vb['f1']:.2f}")
    ba = results["best_thresholds"]["a"]
    va = results["variant_a_sweep"][ba]["score"]
    print(f"Variant A (MiniLM, best):  precision={va['precision']:.2f}  recall={va['recall']:.2f}  f1={va['f1']:.2f}  (t={ba})")
    bc = results["best_thresholds"]["c"]
    vc = results["variant_c_sweep"][bc]["score"]
    print(f"Variant C (MBERT+Haiku):   precision={vc['precision']:.2f}  recall={vc['recall']:.2f}  f1={vc['f1']:.2f}  (t={bc})")
    bd = results["best_thresholds"]["d"]
    vd = results["variant_d_sweep"][bd]["score"]
    print(f"Variant D (MBERT-only):    precision={vd['precision']:.2f}  recall={vd['recall']:.2f}  f1={vd['f1']:.2f}  (t={bd})")
    ve = results["variant_e"]["score"]
    print(f"Variant E (MBERT centroid): precision={ve['precision']:.2f}  recall={ve['recall']:.2f}  f1={ve['f1']:.2f}")
    bf = results["best_thresholds"]["f"]
    vf = results["variant_f_sweep"][bf]["score"]
    print(f"Variant F (MBERT kNN):     precision={vf['precision']:.2f}  recall={vf['recall']:.2f}  f1={vf['f1']:.2f}  (k={bf})")

    print("\nThis is a research spike -- see the printed sections above for full evidence.")
    print("No production wiring was changed by running this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
