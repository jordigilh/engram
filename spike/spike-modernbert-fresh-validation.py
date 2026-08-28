#!/usr/bin/env python3
"""Out-of-sample validation of ModernBERT Variant E (nearest-centroid,
zero LLM cost) against a fresh, never-labeled sample of real transcript
messages (see mine_fresh_messages.py) -- addresses the small-sample-bias
concern with the 19-example held-out eval set in ground_truth.py.

Methodology: hand-labeling 300 fresh examples isn't practical for a spike, so
Variant B (Haiku, no gate) serves as the reference label -- its own accuracy
was already measured at F1=0.97 against real hand-labeled ground truth in
the original 2026-07-08 spike, so treating its judgment as an (imperfect)
reference on fresh data is reasonable, but NOT infallible. Disagreements
between Variant E and Variant B are printed in full for manual spot-check
rather than assumed to always mean Variant E is wrong -- Haiku itself has a
non-zero error rate, and the whole point of this exercise is not to fool
ourselves into false confidence via a new circularity.

Run with the hindsight venv:
    ~/.hindsight/venv/bin/python3 spike/spike-modernbert-fresh-validation.py

Requires spike/mine_fresh_messages.py to have already produced
~/.hindsight/modernbert-spike-cache/fresh_sample.json.

Haiku's labels on this sample are cached to
~/.hindsight/modernbert-spike-cache/fresh_haiku_labels.json (keyed by exact
message text) so re-running this script after a Variant E tweak doesn't
re-spend 300 Haiku calls -- only messages not already in the cache get
classified. Together with mine_fresh_messages.py's fixed sample, this is a
standing regression-check fixture: same sample, same reference labels, every
time this script is re-run, so results are comparable across iterations.
Deliberately NOT under the repo (real transcript content shouldn't land in a
tracked file) but also NOT /tmp (should survive reboots for reuse).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPIKE_DIR = REPO_ROOT / "spike"
sys.path.insert(0, str(SPIKE_DIR))

import modernbert_variants  # noqa: E402
import psycopg2  # noqa: E402
from mine_fresh_messages import CACHE_DIR, CACHE_PATH  # noqa: E402
from modernbert_schema import PG_DSN  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "src"))
from engram.classify import classify_correction  # noqa: E402

HAIKU_CACHE_PATH = os.path.join(CACHE_DIR, "fresh_haiku_labels.json")


def _load_haiku_cache() -> dict:
    if os.path.exists(HAIKU_CACHE_PATH):
        with open(HAIKU_CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_haiku_cache(cache: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(HAIKU_CACHE_PATH, "w") as f:
        json.dump(cache, f)


def classify_with_cache(texts: list[str], cache: dict) -> dict[str, bool]:
    """Returns {text: is_correction}, calling Haiku only for texts not
    already in `cache`. Saves the cache after every new call so a crash or
    interrupt mid-run doesn't lose already-spent calls."""
    labels: dict[str, bool] = {}
    n_cached = 0
    n_fresh = 0
    n_errors = 0
    to_classify = [t for t in texts if t not in cache]
    print(f"  {len(texts) - len(to_classify)} already cached, {len(to_classify)} need classifying...")
    t_start = time.time()
    for i, text in enumerate(texts):
        if text in cache:
            labels[text] = cache[text]["is_correction"]
            n_cached += 1
            continue
        result = classify_correction(text)
        if result.error:
            n_errors += 1
            print(f"    [{i+1}/{len(texts)}] ERROR (retries exhausted): {result.error!r} -- text: {text[:60]!r}")
        cache[text] = {"is_correction": result.is_correction, "category": result.category,
                        "confidence": result.confidence, "error": result.error}
        labels[text] = result.is_correction
        n_fresh += 1
        if n_fresh % 10 == 0:
            elapsed = time.time() - t_start
            rate = n_fresh / elapsed
            remaining = (len(to_classify) - n_fresh) / rate if rate else 0
            print(f"    progress: {n_fresh}/{len(to_classify)} classified "
                  f"({n_errors} errors so far), ~{remaining:.0f}s remaining")
            _save_haiku_cache(cache)  # periodic checkpoint for long runs
    _save_haiku_cache(cache)
    print(f"  Haiku: {n_cached} served from cache, {n_fresh} freshly classified, {n_errors} hard errors.")
    return labels


def hr(title: str = "") -> None:
    print()
    print("=" * 96)
    if title:
        print(title)
        print("=" * 96)


def main() -> int:
    hr("Fresh out-of-sample validation: Variant E (ModernBERT centroid) vs. Variant B (Haiku)")

    with open(CACHE_PATH) as f:
        texts = json.load(f)
    print(f"Loaded {len(texts)} fresh, never-labeled messages from {CACHE_PATH}")

    print("\nRunning Variant B (Haiku, reference label) on all messages "
          "(cached -- only new messages cost a call)...")
    haiku_cache = _load_haiku_cache()
    t0 = time.time()
    b_labels = classify_with_cache(texts, haiku_cache)
    b_elapsed = time.time() - t0
    n_haiku_flagged = sum(b_labels.values())
    print(f"  done in {b_elapsed:.0f}s -- Haiku flagged {n_haiku_flagged}/{len(texts)} "
          f"({100 * n_haiku_flagged / len(texts):.1f}%) as corrections")

    print("\nRunning Variant E (ModernBERT nearest-centroid, zero LLM cost)...")
    conn = psycopg2.connect(PG_DSN)
    t0 = time.time()
    e_results = modernbert_variants.run_variant_e_centroid(texts, conn=conn)
    e_elapsed = time.time() - t0
    conn.close()
    e_labels = {r.text: r.predicted_correction for r in e_results}
    n_e_flagged = sum(e_labels.values())
    print(f"  done in {e_elapsed:.1f}s -- Variant E flagged {n_e_flagged}/{len(texts)} "
          f"({100 * n_e_flagged / len(texts):.1f}%) as corrections")

    hr("Agreement analysis (Haiku as reference)")
    agree = sum(1 for t in texts if b_labels[t] == e_labels[t])
    disagree = [t for t in texts if b_labels[t] != e_labels[t]]
    print(f"Agreement rate: {agree}/{len(texts)} = {100 * agree / len(texts):.1f}%")
    print(f"Disagreements: {len(disagree)}")

    # Treat Haiku's label as reference truth to get a precision/recall proxy
    # for Variant E on fresh data -- explicitly a proxy, not ground truth.
    tp = sum(1 for t in texts if b_labels[t] and e_labels[t])
    fp = sum(1 for t in texts if not b_labels[t] and e_labels[t])
    fn = sum(1 for t in texts if b_labels[t] and not e_labels[t])
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    print(f"\nVariant E vs. Haiku-as-reference: precision={precision:.2f} recall={recall:.2f} f1={f1:.2f}")
    print("(This is E's agreement with Haiku's judgment, NOT accuracy against real ground")
    print("truth -- Haiku itself has a non-zero error rate. See disagreements below.)")

    hr(f"All {len(disagree)} disagreements, for manual spot-check")
    for t in disagree:
        e_res = e_labels[t]
        b_res = b_labels[t]
        print(f"  Haiku={b_res!s:5s} ModernBERT-E={e_res!s:5s}  {t[:100]!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
