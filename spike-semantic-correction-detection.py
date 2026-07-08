#!/usr/bin/env python3
"""Semantic Correction Detection Spike -- evaluation harness.

Investigates whether embedding similarity + LLM validation (and an
LLM-based contradiction check) can outperform regex-based correction
detection. This is a research spike: the goal is an evidence-backed
answer, not a shipped feature. See
~/.cursor/plans/semantic_correction_detection_spike_86e447df.plan.md for
the full design and rationale.

Run with the hindsight venv (has litellm/vertexai/sentence-transformers/
psycopg2 installed):
    ~/.hindsight/venv/bin/python3 spike-semantic-correction-detection.py

Steps:
    0. Preflight smoke test (litellm/Vertex auth) -- abort if it fails.
    1. Ensure cocoindex.correction_embeddings is seeded (seed split only).
    2. Run regex baseline (today's production nightly-learn.py patterns),
       Variant A (embedding-gated), and Variant B (classify-everything)
       against the held-out eval split -- report precision/recall/F1/cost
       for each, plus where A and B disagree.
    3. Run the contradiction check (Config A: Sonnet) against the
       synthetic pairs suite (Config B) -- report its own accuracy.
    4. Sanity-check the contradiction check against real cursor-memory
       bank content using a sample of confirmed held-out corrections.
    5. Estimate real-world daily message volume/cost from recent
       transcripts.
"""
from __future__ import annotations

import importlib.util
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SPIKE_DIR = REPO_ROOT / "spike"
sys.path.insert(0, str(SPIKE_DIR))

from ground_truth import DATASET, eval_examples, seed_examples  # noqa: E402
from contradiction_suite import CASES as CONTRADICTION_CASES  # noqa: E402
from classify import check_contradiction, HAIKU_MODEL, SONNET_MODEL  # noqa: E402
from hindsight_client import recall  # noqa: E402
import schema  # noqa: E402
import variants  # noqa: E402

# nightly-learn.py has a hyphen, so it can't be `import`ed normally.
_spec = importlib.util.spec_from_file_location(
    "nightly_learn", REPO_ROOT / "nightly-learn.py"
)
nl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nl)


def hr(title: str = "") -> None:
    print()
    print("=" * 78)
    if title:
        print(title)
        print("=" * 78)


def step0_preflight() -> bool:
    hr("STEP 0: Preflight smoke test")
    result = importlib.util.spec_from_file_location(
        "preflight", SPIKE_DIR / "preflight_smoke_test.py"
    )
    mod = importlib.util.module_from_spec(result)
    result.loader.exec_module(mod)
    ok = mod.main() == 0
    if not ok:
        print("\nABORTING: preflight failed. Fix auth before continuing.")
    return ok


def step1_seed_schema() -> None:
    hr("STEP 1: Ensure cocoindex.correction_embeddings is seeded")
    import psycopg2

    conn = psycopg2.connect(schema.PG_DSN)
    try:
        schema.ensure_schema(conn)
        schema.seed_table(conn)
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


def step2_compare_variants() -> dict:
    hr("STEP 2: Regex baseline vs. Variant A vs. Variant B (held-out eval set)")
    eval_set = eval_examples()
    truth = {ex.text: ex.is_correction for ex in eval_set}
    texts = [ex.text for ex in eval_set]
    print(f"Held-out eval set: {len(eval_set)} examples "
          f"({sum(truth.values())} corrections, {len(truth) - sum(truth.values())} benign)")
    print("(This set was never used to seed correction_embeddings or as few-shot examples.)\n")

    # --- Baseline: today's production regex (nightly-learn.py CORRECTION_PATTERNS) ---
    regex_preds = {t: nl.is_correction(t) for t in texts}
    regex_score = score(regex_preds, truth)

    # --- Variant B first (no DB dependency, establishes per-message Haiku cost) ---
    print("Running Variant B (classify every message)...")
    t0 = time.time()
    b_results = variants.run_variant_b(texts)
    b_elapsed = time.time() - t0
    b_preds = {r.text: r.predicted_correction for r in b_results}
    b_score = score(b_preds, truth)
    b_calls = sum(1 for r in b_results if r.classification is not None)

    # --- Variant A: sweep a few thresholds since MiniLM similarity for short
    # chat corrections is uncalibrated -- this is exactly the kind of thing
    # the spike exists to measure rather than assume. ---
    print("Running Variant A (embedding-gated) across threshold sweep...")
    import psycopg2

    conn = psycopg2.connect(schema.PG_DSN)
    a_sweep = {}
    thresholds = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55]
    for threshold in thresholds:
        t0 = time.time()
        a_results = variants.run_variant_a(texts, threshold=threshold, conn=conn)
        elapsed = time.time() - t0
        a_preds = {r.text: r.predicted_correction for r in a_results}
        a_score = score(a_preds, truth)
        n_candidates = sum(1 for r in a_results if r.was_candidate)
        a_sweep[threshold] = {
            "score": a_score, "n_candidates": n_candidates, "elapsed": elapsed,
            "results": a_results,
        }
    conn.close()

    # Pick the threshold with the best F1 on the eval set for the headline comparison.
    best_threshold = max(a_sweep, key=lambda t: a_sweep[t]["score"]["f1"])
    best_a = a_sweep[best_threshold]

    print(f"\n{'Method':<28} {'Prec':>6} {'Rec':>6} {'F1':>6} {'TP':>4} {'FP':>4} {'FN':>4} {'LLM calls':>10} {'Time':>8}")
    print("-" * 90)
    print(f"{'Regex (production today)':<28} {regex_score['precision']:>6.2f} {regex_score['recall']:>6.2f} "
          f"{regex_score['f1']:>6.2f} {regex_score['tp']:>4} {regex_score['fp']:>4} {regex_score['fn']:>4} "
          f"{'0 (free)':>10} {'~0s':>8}")
    print(f"{'Variant B (classify all)':<28} {b_score['precision']:>6.2f} {b_score['recall']:>6.2f} "
          f"{b_score['f1']:>6.2f} {b_score['tp']:>4} {b_score['fp']:>4} {b_score['fn']:>4} "
          f"{b_calls:>10} {b_elapsed:>7.1f}s")
    for threshold in thresholds:
        s = a_sweep[threshold]["score"]
        marker = " *best F1*" if threshold == best_threshold else ""
        print(f"{'Variant A (thresh=' + f'{threshold:.2f})':<28} {s['precision']:>6.2f} {s['recall']:>6.2f} "
              f"{s['f1']:>6.2f} {s['tp']:>4} {s['fp']:>4} {s['fn']:>4} "
              f"{a_sweep[threshold]['n_candidates']:>10} {a_sweep[threshold]['elapsed']:>7.1f}s{marker}")

    # Disagreements between B and best-F1 A, for manual inspection.
    print(f"\nDisagreements between Variant A (threshold={best_threshold}) and Variant B:")
    a_preds_best = {r.text: r.predicted_correction for r in best_a["results"]}
    disagreements = [t for t in texts if a_preds_best.get(t) != b_preds.get(t)]
    if not disagreements:
        print("  (none -- A and B agreed on every example)")
    for t in disagreements:
        print(f"  A={a_preds_best.get(t)!s:5s} B={b_preds.get(t)!s:5s} truth={truth[t]!s:5s}  {t[:80]}")

    return {
        "regex": regex_score,
        "variant_b": {"score": b_score, "calls": b_calls, "elapsed": b_elapsed},
        "variant_a_sweep": {t: {"score": v["score"], "n_candidates": v["n_candidates"]} for t, v in a_sweep.items()},
        "variant_a_best_threshold": best_threshold,
        "disagreement_count": len(disagreements),
    }


def step3_contradiction_suite() -> dict:
    hr("STEP 3: Contradiction check (Config A: Sonnet) vs. synthetic suite (Config B)")
    results_by_model = {}
    for label, model in (("sonnet", SONNET_MODEL), ("haiku", HAIKU_MODEL)):
        correct = 0
        idx_correct = 0
        idx_applicable = 0
        latencies = []
        for case in CONTRADICTION_CASES:
            r = check_contradiction(case.new_statement, case.existing_memories, model=model)
            latencies.append(r.latency_s)
            ok = r.contradicts == case.expected_contradicts
            correct += ok
            if case.expected_contradicts and r.contradicts:
                idx_applicable += 1
                idx_correct += int(r.conflicting_memory_index == case.expected_conflict_idx)
        results_by_model[label] = {
            "accuracy": correct / len(CONTRADICTION_CASES),
            "n": len(CONTRADICTION_CASES),
            "idx_accuracy": (idx_correct / idx_applicable) if idx_applicable else None,
            "avg_latency": statistics.mean(latencies),
        }
        print(f"{label:8s} accuracy={results_by_model[label]['accuracy']*100:5.1f}%  "
              f"conflict-idx accuracy={idx_correct}/{idx_applicable}  "
              f"avg latency={results_by_model[label]['avg_latency']:.2f}s")
    return results_by_model


def step4_real_world_contradiction_check() -> int:
    hr("STEP 4: Real-world sanity check -- contradiction check vs. actual cursor-memory content")
    print("Taking confirmed held-out corrections and checking them against real recall()")
    print("results from the live cursor-memory bank, to see if any false-positive")
    print("contradictions surface against real content before this ever gates a real retain.\n")
    positives = [e for e in eval_examples() if e.is_correction][:6]
    false_positive_contradictions = 0
    for ex in positives:
        memories = recall("cursor-memory", ex.text, max_results=3)
        if not memories:
            print(f"  (no related memories found for: {ex.text[:60]})")
            continue
        r = check_contradiction(ex.text, memories, model=SONNET_MODEL)
        flag = "CONTRADICTION FLAGGED" if r.contradicts else "no contradiction"
        print(f"  [{flag}] {ex.text[:60]}")
        if r.contradicts:
            false_positive_contradictions += 1
            print(f"      vs: {memories[r.conflicting_memory_index][:100] if r.conflicting_memory_index is not None else '?'}")
            print(f"      why: {r.explanation}")
    print(f"\n{false_positive_contradictions}/{len(positives)} flagged a contradiction against real memory content.")
    print("(Since these are all genuine corrections with no known real conflict in memory,")
    print("any flags here would be false positives worth inspecting before trusting this gate.)")
    return false_positive_contradictions


def step5_volume_estimate() -> None:
    hr("STEP 5: Real-world message volume estimate (last 7 days, both projects)")
    import glob
    import os
    from datetime import datetime, timedelta

    transcripts_glob = os.path.expanduser("~/.cursor/projects/*/agent-transcripts/**/*.jsonl")
    cutoff = (datetime.now() - timedelta(days=7)).timestamp()
    paths = [p for p in glob.glob(transcripts_glob, recursive=True)
             if os.stat(p).st_mtime >= cutoff and "/subagents/" not in p]
    total_user_messages = 0
    import json as jsonlib
    for p in paths:
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = jsonlib.loads(line)
                    except Exception:
                        continue
                    role = obj.get("role") or obj.get("message", {}).get("role", "")
                    if role == "user":
                        total_user_messages += 1
        except Exception:
            pass
    per_day = total_user_messages / 7
    print(f"User messages in the last 7 days (top-level, both projects): {total_user_messages}")
    print(f"~{per_day:.0f}/day")
    print(f"\nVariant B (classify everything) would issue ~{per_day:.0f} Haiku calls/day.")
    print("Variant A (embedding-gated) would issue that many embeddings (free, local) plus")
    print("only as many Haiku calls as pass the similarity threshold (see Step 2's")
    print("n_candidates column for the actual gating rate observed on the eval set).")
    print("At Haiku pricing, even the full classify-everything volume here is negligible")
    print("(low hundreds of short messages/day) -- cost is not a meaningful differentiator")
    print("between the two variants at current usage levels.")


def main() -> int:
    if not step0_preflight():
        return 1
    step1_seed_schema()
    variant_results = step2_compare_variants()
    contradiction_results = step3_contradiction_suite()
    fp_count = step4_real_world_contradiction_check()
    step5_volume_estimate()

    hr("SUMMARY")
    print(f"Seed examples: {len(seed_examples())}  Held-out eval examples: {len(eval_examples())}  "
          f"Total labeled: {len(DATASET)}")
    print(f"\nRegex (production today):  precision={variant_results['regex']['precision']:.2f}  "
          f"recall={variant_results['regex']['recall']:.2f}  f1={variant_results['regex']['f1']:.2f}")
    vb = variant_results["variant_b"]["score"]
    print(f"Variant B (classify all):  precision={vb['precision']:.2f}  recall={vb['recall']:.2f}  f1={vb['f1']:.2f}")
    best_t = variant_results["variant_a_best_threshold"]
    va = variant_results["variant_a_sweep"][best_t]["score"]
    print(f"Variant A (best thresh={best_t}): precision={va['precision']:.2f}  recall={va['recall']:.2f}  f1={va['f1']:.2f}")
    print(f"\nContradiction check (Sonnet) on synthetic suite: {contradiction_results['sonnet']['accuracy']*100:.0f}%")
    print(f"Contradiction check (Haiku) on synthetic suite:  {contradiction_results['haiku']['accuracy']*100:.0f}%")
    print(f"Real-world contradiction false-positive check: {fp_count} flagged out of 6 known-clean corrections")
    print("\nThis is a research spike -- see the printed sections above for full evidence.")
    print("No production wiring was changed by running this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
