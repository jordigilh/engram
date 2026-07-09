"""One-off validation for the correction-detection prompt v1 -> v2 change
(see docs/FINDINGS.md 2026-07-09). Runs BOTH prompt versions against:

  1. ground_truth.py's held-out eval split (never used to write either
     prompt version) -- confirms v2 doesn't regress recall on the original
     hand-labeled corrections.
  2. A manually-labeled sample of live-traffic messages that v1 flagged as
     corrections but a human reviewer judged were not (task assignments,
     requirement statements, open questions, TODO reminders) -- confirms v2
     actually fixes the false positives it was written for.

Not part of the ongoing shadow trial; run manually when validating a prompt
change, then discard/archive the result into docs/FINDINGS.md.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import classify  # noqa: E402
from ground_truth import DATASET  # noqa: E402

# v1 prompt, frozen here for comparison (classify.py now has v2 as the live
# _CORRECTION_SYSTEM_PROMPT).
_V1_PROMPT = """You are analyzing a single message a human sent to an AI coding assistant during a work session. Decide whether this message is CORRECTING the assistant for something it did or said wrong.

A correction includes (non-exhaustive): pointing out a methodology/process violation, a convention violation ("we don't use X here"), a factual/technical error, an unwanted or unauthorized action the assistant took, a request to undo/revert something, or calling out a repeated mistake.

NOT a correction: the human correcting THEMSELVES (self-reflection, "I misunderstood"), a plain question, a status update, a dismissal ("nevermind", "it's fine"), or an observation that doesn't assign fault to the assistant's prior action.

Respond with ONLY a JSON object, no other text:
{"is_correction": true or false, "category": "short_snake_case_label or null", "confidence": 0.0-1.0}"""

# 30 messages from the 80-message random-sample manual triage (2026-07-09)
# that v1's classify_correction() flagged is_correction=true, but manual
# human review judged were NOT genuine corrections (new task assignments,
# requirement/scope statements, open design questions, TODO reminders).
FALSE_POSITIVE_SAMPLE = [
    "leave them for amd64 only, I don't want to extend the build time more than needed",
    "I think we don't need thanos here, just alert manager to attach the cluster_id should be sufficient",
    "why only datastore? doesn't KA and GW have that also?",
    "comment looks good, but the doc content needs to reflect that :\n* multi tenancy is out of scope and explain the dependencies",
    "did you also update the authoritative documentation to make sure any reference is consistent?",
    "we won't be using goose here, agents will be packaged as OCI and run as containers on top of openshell, we are runtime agnostic",
    "FMC should reuse the redis instance for DS",
    "should we have a dedicated memory bank or consolidate in the same memory banks that we use for kubernaut?",
    "yes, follow the methodology to the letter",
    "use gh cli",
    "commit in logical groups and create a PR",
    "plan using the project's methdology",
    "I'd rather have 2 phased like we do for the workflows:\n* select cluster\n* list tools",
    "can't you recall using hindsight for kubernaut issues?",
    "1 can't you derive it from existing code?",
    "can you rewrite it so that it has the similar format and structure we use in the comments in this project?",
    ">VM-as-a-Service (deferred):\ncan we back this up with any authoritative reference in OSAC?",
    "we have to update the documentation first",
    "can we organize it better? we're mixing FMC specs with MCP Gateways",
    "why not a simple regex?",
    "proceed following the project's methodology",
    "you will still have to add jordigilh the cpell.yaml",
    "leave kubernaut-operator out of this plan",
    "store them under assets/",
    "why in the troubleshooting section? is it something that should be part of the installation?",
    "check that we're using the correct context",
    "we should have ITs for both gateways",
    "triage again all services that you have discarded with no remote k8s",
    "can we restructure this to match the API convention used elsewhere?",
    "Implement the plan as specified, it is attached for your reference. Do NOT edit the plan file itself.",
]


def classify_with_prompt(text: str, prompt: str) -> dict:
    import litellm

    resp = litellm.completion(
        model=classify.HAIKU_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Message:\n{text[:1500]}"},
        ],
        max_tokens=150,
        temperature=0,
        timeout=30,
    )
    raw = resp.choices[0].message.content
    return classify._extract_json(raw)


def eval_ground_truth():
    eval_examples = [ex for ex in DATASET if ex.split == "eval"]
    print(f"Ground truth eval split: {len(eval_examples)} examples "
          f"({sum(1 for e in eval_examples if e.is_correction)} corrections, "
          f"{sum(1 for e in eval_examples if not e.is_correction)} benign)")

    results = {"v1": {}, "v2": {}}
    for version, prompt in [("v1", _V1_PROMPT), ("v2", classify._CORRECTION_SYSTEM_PROMPT)]:
        preds = {}
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(classify_with_prompt, ex.text, prompt): ex for ex in eval_examples}
            for fut in as_completed(futures):
                ex = futures[fut]
                try:
                    parsed = fut.result()
                    preds[ex.text] = bool(parsed.get("is_correction", False))
                except Exception as e:
                    print(f"  ERROR on {ex.text[:50]!r}: {e}")
                    preds[ex.text] = None

        tp = sum(1 for ex in eval_examples if ex.is_correction and preds.get(ex.text) is True)
        fn = sum(1 for ex in eval_examples if ex.is_correction and preds.get(ex.text) is False)
        fp = sum(1 for ex in eval_examples if not ex.is_correction and preds.get(ex.text) is True)
        tn = sum(1 for ex in eval_examples if not ex.is_correction and preds.get(ex.text) is False)
        recall = tp / (tp + fn) if (tp + fn) else 0
        precision = tp / (tp + fp) if (tp + fp) else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0
        results[version] = {"tp": tp, "fn": fn, "fp": fp, "tn": tn,
                            "recall": recall, "precision": precision, "f1": f1}
        print(f"\n{version}: TP={tp} FN={fn} FP={fp} TN={tn}  "
              f"recall={recall:.2f} precision={precision:.2f} f1={f1:.2f}")
        if fn:
            print(f"  MISSED corrections ({version}):")
            for ex in eval_examples:
                if ex.is_correction and preds.get(ex.text) is False:
                    print(f"    [{ex.category}] {ex.text[:90]!r}")
    return results


def eval_false_positive_sample():
    print(f"\n\nLive-traffic false-positive sample: {len(FALSE_POSITIVE_SAMPLE)} messages "
          f"(all human-judged NOT corrections, all flagged True by v1)")
    for version, prompt in [("v1", _V1_PROMPT), ("v2", classify._CORRECTION_SYSTEM_PROMPT)]:
        flagged_true = 0
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(classify_with_prompt, text, prompt): text for text in FALSE_POSITIVE_SAMPLE}
            still_flagged = []
            for fut in as_completed(futures):
                text = futures[fut]
                try:
                    parsed = fut.result()
                    if parsed.get("is_correction"):
                        flagged_true += 1
                        still_flagged.append(text)
                except Exception as e:
                    print(f"  ERROR on {text[:50]!r}: {e}")
        pct = flagged_true / len(FALSE_POSITIVE_SAMPLE) * 100
        print(f"{version}: still flagged as correction: {flagged_true}/{len(FALSE_POSITIVE_SAMPLE)} = {pct:.0f}%")
        if version == "v2" and still_flagged:
            print("  Still flagged by v2:")
            for t in still_flagged:
                print(f"    {t[:90]!r}")


if __name__ == "__main__":
    eval_ground_truth()
    eval_false_positive_sample()
