"""LLM classification calls shared by both pipeline variants (litellm/Vertex).

Two calls:
  - classify_correction(): "is this a correction?" -- used by both Variant A
    (only on candidates that pass the embedding-similarity gate) and Variant B
    (on every message, no gate). Uses Haiku -- cheap, high volume.
  - check_contradiction(): "does this new statement contradict an existing
    memory?" -- used only after a message is confirmed as a correction. Uses
    Sonnet (Config A) since it's low-volume/high-stakes.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

os.environ.setdefault("VERTEXAI_PROJECT", "example-gcp-project")
os.environ.setdefault("VERTEXAI_LOCATION", "global")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "example-gcp-project")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.expanduser("~/.config/gcloud/application_default_credentials.json"),
)

HAIKU_MODEL = "vertex_ai/claude-haiku-4-5@20251001"
SONNET_MODEL = "vertex_ai/claude-sonnet-4-6"

# v2, 2026-07-09: v1 had ~42% false positives on live traffic (manual review
# of an 80-message random sample of Haiku's "is_correction=true" output --
# see docs/FINDINGS.md). The failure wasn't ambiguous edge cases, it was
# whole classes of message v1's negative examples didn't cover: new task/
# plan assignments, forward-looking requirement/scope statements, open
# design questions, and TODO-style reminders -- all of which sound
# instructional or critical without actually asserting the assistant did
# something wrong. v2 adds explicit negative examples for each pattern
# found and a strict fault-assignment test. Re-validated against
# ground_truth.py's eval split (never used to write these examples) before
# and after this change to confirm no recall regression -- see
# docs/FINDINGS.md 2026-07-09 for the before/after numbers.
_CORRECTION_SYSTEM_PROMPT = """You are analyzing a single message a human sent to an AI coding assistant during a work session. Decide whether this message is CORRECTING the assistant for something it did or said wrong.

A correction must assign fault to something the assistant ALREADY did or said -- not merely describe future/desired work. It includes (non-exhaustive): pointing out a methodology/process violation, a convention violation ("we don't use X here"), a factual/technical error, an unwanted or unauthorized action the assistant took, a request to undo/revert something, or calling out a repeated mistake.

IMPORTANT EXCEPTION -- still IS a correction even though phrased as a forward directive: "we don't use X", "do not use X", "we don't do X here", "that's not how we do it" -- these assert an EXISTING convention that the assistant's current/just-proposed approach conflicts with. Treat these as corrections regardless of imperative phrasing; do not let the "new task assignment" rule below override this.

NOT a correction (these are commonly confused with corrections -- read carefully):
- The human correcting THEMSELVES (self-reflection, "I misunderstood").
- A plain question, INCLUDING ones that sound critical ("why not a simple regex?", "why skip TLS verify?", "can we organize it better?") -- unless the human also explicitly asserts the current thing is wrong/incorrect, not just asks for justification or floats an alternative.
- A NEW task or plan assignment unrelated to any existing convention ("implement the plan as specified", "add integration tests for both gateways", "create an issue to track this") -- assigning brand-new work is not correcting prior work, even when phrased as an imperative instruction. This does NOT apply to the "we don't use X" exception above.
- A requirement, scope, or preference statement that doesn't reference a specific thing the assistant already did wrong AND doesn't invoke an existing convention ("leave this for amd64 only", "one diagram per lane", "we should have ITs for both gateways", "I'd rather have it phased like X").
- A reminder, TODO, or status-check about something not yet done, when nothing indicates the assistant previously claimed it was already done ("you'll still need to add X", "check that we're using the right context", "we still have comments unaddressed").
- A status update, a dismissal ("nevermind", "it's fine"), or an observation that doesn't assign fault to the assistant's prior action.

When genuinely uncertain whether something is a correction versus a plain instruction/question/requirement, prefer NOT a correction -- false negatives here are cheaper than false positives. This tie-break does NOT apply to the "we don't use X" exception above, which should lean toward IS a correction.

Respond with ONLY a JSON object, no other text:
{"is_correction": true or false, "category": "short_snake_case_label or null", "confidence": 0.0-1.0}"""

_CONTRADICTION_SYSTEM_PROMPT = """You are checking whether a NEW statement from a user contradicts any of a list of EXISTING memories already stored about their project preferences/conventions/facts.

A contradiction means the new statement asserts something that is factually incompatible with an existing memory (not just a refinement, elaboration, unrelated topic, or a more specific case of the same rule).

Also rate your confidence that "contradicts" is correct. Use a LOWER confidence (below 0.9) when the case is a narrow exception, scoping change, or partial overlap rather than a clear-cut factual conflict -- this signal is used to decide whether a contradiction is safe to auto-resolve without human review, so it must reflect genuine uncertainty, not just be a high number by default.

Respond with ONLY a JSON object, no other text:
{"contradicts": true or false, "conflicting_memory_index": <int index into the existing memories list, or null>, "explanation": "one sentence", "confidence": 0.0-1.0}"""


@dataclass
class ClassificationResult:
    is_correction: bool
    category: str | None
    confidence: float
    raw: str
    latency_s: float
    error: str | None = None


@dataclass
class ContradictionResult:
    contradicts: bool
    conflicting_memory_index: int | None
    explanation: str
    raw: str
    latency_s: float
    confidence: float = 0.0
    error: str | None = None


def _extract_json(text: str) -> dict:
    text = text.strip()
    # Models occasionally wrap JSON in a code fence despite instructions.
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"no JSON object found in response: {text!r}")
    return json.loads(match.group(0))


def classify_correction(text: str, model: str = HAIKU_MODEL, retries: int = 2) -> ClassificationResult:
    import litellm

    prompt = f"Message:\n{text[:1500]}"
    t0 = time.time()
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = litellm.completion(
                model=model,
                messages=[
                    {"role": "system", "content": _CORRECTION_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=150,
                temperature=0,
                timeout=30,
            )
            raw = resp.choices[0].message.content
            parsed = _extract_json(raw)
            return ClassificationResult(
                is_correction=bool(parsed.get("is_correction", False)),
                category=parsed.get("category"),
                confidence=float(parsed.get("confidence", 0.0)),
                raw=raw,
                latency_s=time.time() - t0,
            )
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    return ClassificationResult(
        is_correction=False, category=None, confidence=0.0, raw="",
        latency_s=time.time() - t0, error=str(last_err),
    )


def check_contradiction(
    new_statement: str,
    existing_memories: list[str],
    model: str = SONNET_MODEL,
    retries: int = 2,
) -> ContradictionResult:
    import litellm

    if not existing_memories:
        return ContradictionResult(
            contradicts=False, conflicting_memory_index=None,
            explanation="no existing memories to compare against", raw="", latency_s=0.0,
            confidence=1.0,
        )

    memory_list = "\n".join(f"[{i}] {m[:300]}" for i, m in enumerate(existing_memories))
    prompt = f"NEW statement:\n{new_statement[:800]}\n\nEXISTING memories:\n{memory_list}"

    t0 = time.time()
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = litellm.completion(
                model=model,
                messages=[
                    {"role": "system", "content": _CONTRADICTION_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=200,
                temperature=0,
                timeout=30,
            )
            raw = resp.choices[0].message.content
            parsed = _extract_json(raw)
            return ContradictionResult(
                contradicts=bool(parsed.get("contradicts", False)),
                conflicting_memory_index=parsed.get("conflicting_memory_index"),
                explanation=parsed.get("explanation", ""),
                raw=raw,
                latency_s=time.time() - t0,
                confidence=float(parsed.get("confidence", 0.0)),
            )
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    return ContradictionResult(
        contradicts=False, conflicting_memory_index=None, explanation="",
        raw="", latency_s=time.time() - t0, confidence=0.0, error=str(last_err),
    )
