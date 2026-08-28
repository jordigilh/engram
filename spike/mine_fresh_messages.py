"""Mine a fresh, never-labeled sample of real user messages from Cursor
transcripts for out-of-sample validation of the ModernBERT correction
classifier (spike/spike-modernbert-correction-detection.py).

Purpose: the 19-example held-out eval set in ground_truth.py is small enough
that a strong result there could be partly luck / overfitting to that
specific sample's phrasing. This pulls a much larger sample (default 300) of
real messages the classifier has never seen, EXCLUDING anything already in
ground_truth.DATASET (seed or eval) to avoid any overlap.

This does NOT produce hand-labeled ground truth -- see
spike-modernbert-fresh-validation.py for how the sample is scored (Variant B/
Haiku as the reference label, since hand-labeling 300 examples isn't
practical for a spike; Haiku's own accuracy against real ground truth was
already measured at F1=0.97 in the original spike, so it's a reasonable,
though imperfect, reference -- disagreements get manually spot-checked, not
assumed to always mean the classifier under test is wrong).

This sample + its cached Haiku reference labels (see
spike-modernbert-fresh-validation.py's HAIKU_CACHE_PATH) together form a
standing regression-check fixture for future ModernBERT variant changes --
re-running the validation script after any classifier tweak reuses this same
fixed sample and its already-spent Haiku calls, so results are directly
comparable across iterations without re-paying for classification.

Output is cached under ~/.hindsight/ (deliberately NOT under the repo, and
NOT /tmp so it survives reboots) since real transcript content shouldn't be
persisted into a tracked file, but should stick around for reuse.
"""
from __future__ import annotations

import glob
import json
import os
import random
import sys

import re

sys.path.insert(0, os.path.dirname(__file__))
from ground_truth import DATASET  # noqa: E402

_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)

CACHE_DIR = os.path.expanduser("~/.hindsight/modernbert-spike-cache")
CACHE_PATH = os.path.join(CACHE_DIR, "fresh_sample.json")

_KNOWN_TEXTS = {ex.text for ex in DATASET}

# Cheap boilerplate/near-empty filters -- not trying to be exhaustive, just
# excluding the obvious noise the 2026-07-08 prefilter-shadow-trial found
# (system-injected boilerplate attributed to the user role, bare
# acknowledgments) so the sample is representative of real judgment calls.
_TRIVIAL_EXACT = {
    "yes", "no", "ok", "okay", "sounds good", "continue", "go ahead",
    "yes please", "sure", "thanks", "thank you", "lgtm", "approved",
}


def _iter_user_messages():
    pattern = os.path.expanduser("~/.cursor/projects/*/agent-transcripts/**/*.jsonl")
    paths = [p for p in glob.glob(pattern, recursive=True) if "/subagents/" not in p]
    for p in paths:
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    role = obj.get("role") or obj.get("message", {}).get("role", "")
                    if role != "user":
                        continue
                    text = obj.get("text") or obj.get("message", {}).get("content", "")
                    if isinstance(text, list):
                        # Some transcript formats store content as a list of blocks.
                        text = " ".join(
                            b.get("text", "") for b in text if isinstance(b, dict)
                        )
                    if not isinstance(text, str):
                        continue
                    yield text.strip()
        except Exception:
            continue


def _clean(text: str) -> str | None:
    """Returns cleaned text, or None if this message should be dropped
    entirely.

    Cursor wraps the real message in a `<user_query>...</user_query>` tag
    whenever other system-injected context (image-upload boilerplate,
    `<timestamp>`, `<attached_files>`, etc.) is also present in the same
    turn -- found empirically (2026-08-25) that this combo dominates the raw
    candidate pool (100% of an unfiltered 300-sample draw had an
    `[Image]...` prefix), the same class of "system-injected content
    attributed to the user role" contamination documented in the original
    2026-07-08 prefilter-shadow-trial finding. Prefer the `<user_query>`
    contents when present; they're always the actual human text regardless
    of what surrounds them.
    """
    m = _USER_QUERY_RE.search(text)
    if m:
        text = m.group(1).strip()
    # A message that is *itself* just a bare system-injected tag block (no
    # `<user_query>` wrapper found above) -- crude but effective per the
    # original shadow-trial fix.
    if text.startswith("<") and text.endswith(">"):
        return None
    return text


def mine(sample_size: int = 300, seed: int = 7, exclude: set[str] | None = None) -> list[str]:
    """`exclude` lets a second/third mining round pull only messages not
    already in a prior batch (e.g. the original 300), so growing the
    training corpus doesn't just re-draw the same pool with a new seed."""
    exclude = exclude or set()
    seen = set()
    candidates = []
    for raw_text in _iter_user_messages():
        if not raw_text:
            continue
        text = _clean(raw_text)
        if text is None or not text or text in seen:
            continue
        if text in _KNOWN_TEXTS or text in exclude:
            continue  # never sample anything already in the labeled dataset
        if len(text) < 10 or len(text) > 2000:
            continue
        if text.lower() in _TRIVIAL_EXACT:
            continue
        seen.add(text)
        candidates.append(text)

    print(f"Found {len(candidates)} unique, never-labeled candidate messages "
          f"across all transcripts.")
    rng = random.Random(seed)
    rng.shuffle(candidates)
    sample = candidates[:sample_size]
    print(f"Sampled {len(sample)} for fresh out-of-sample validation.")
    return sample


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-size", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--force", action="store_true", help="Re-mine even if cache exists")
    args = ap.parse_args()

    if os.path.exists(CACHE_PATH) and not args.force:
        with open(CACHE_PATH) as f:
            cached = json.load(f)
        print(f"Cache already exists at {CACHE_PATH} ({len(cached)} messages) -- use --force to re-mine.")
        return

    os.makedirs(CACHE_DIR, exist_ok=True)
    sample = mine(args.sample_size, args.seed)
    with open(CACHE_PATH, "w") as f:
        json.dump(sample, f)
    print(f"Wrote {len(sample)} messages to {CACHE_PATH}")


if __name__ == "__main__":
    main()
