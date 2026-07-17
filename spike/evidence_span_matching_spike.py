#!/usr/bin/env python3
"""Spike: can a cheap, local, no-LLM check verify that a live-flagged
`evidence_span` actually appears in the real transcript, given real Cursor
transcript formatting (nested content blocks, escaped unicode, embedded
<timestamp>/<user_query> tags, multi-line text)?

This validates step 2 of the "live in-loop flag" design discussed in chat
(2026-07-16): the agent's flag tool should reject any evidence_span that
isn't a literal substring of recent transcript text, with zero LLM cost.
The open risk before this spike: does a naive substring match actually
survive realistic formatting noise, or does it need normalization (and if
so, how much normalization before it stops being a meaningful check)?

Source data: this chat's own live transcript file (see agent-transcripts/),
which is real production JSONL, not synthetic.

Run: python3 spike/evidence_span_matching_spike.py
"""
from __future__ import annotations

import glob
import json
import re
from dataclasses import dataclass


def load_real_transcript_texts(max_files: int = 5, max_msgs: int = 200) -> list[str]:
    """Pull real `message.content[].text` strings from this workspace's own
    agent-transcripts/ JSONL files -- real production formatting, not a mock."""
    pattern = (
        "/Users/jgil/.cursor/projects/"
        "Users-jgil-go-src-github-com-jordigilh-engram/agent-transcripts/**/*.jsonl"
    )
    texts: list[str] = []
    for path in sorted(glob.glob(pattern, recursive=True))[:max_files]:
        with open(path) as f:
            for line in f:
                if len(texts) >= max_msgs:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for block in obj.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        texts.append(block["text"])
    return texts


def exact_substring_match(evidence_span: str, haystack: str) -> bool:
    """Zero-normalization check: literal substring, byte-for-byte."""
    return evidence_span in haystack


def whitespace_normalized_match(evidence_span: str, haystack: str) -> bool:
    """Collapse runs of whitespace before comparing -- the minimal
    normalization real transcript text plausibly needs (line-wrapping,
    trailing spaces) without opening the door to loose/fuzzy matching."""
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    return norm(evidence_span) in norm(haystack)


@dataclass
class MatchCase:
    label: str
    evidence_span: str
    haystack: str
    expected_match: bool
    note: str


def build_synthetic_edge_cases(real_texts: list[str]) -> list[MatchCase]:
    """Perturbation cases modeling the specific formatting risks named in
    chat: whitespace differences, boundary-spanning quotes, and paraphrase
    (which SHOULD fail -- paraphrase is exactly what the mechanical check
    must reject, since only a real Sonnet/Haiku call should judge meaning)."""
    cases: list[MatchCase] = []

    # Real excerpt, verbatim -- must match under both functions.
    for t in real_texts:
        if len(t) > 80 and "\n" not in t[:80]:
            cases.append(MatchCase(
                label="verbatim-real",
                evidence_span=t[10:70],
                haystack=t,
                expected_match=True,
                note="Verbatim substring of real transcript text",
            ))
            break

    # Real excerpt with a trailing space added by the "agent" when quoting.
    if real_texts:
        base = real_texts[0]
        snippet = base[:60] if len(base) > 60 else base
        cases.append(MatchCase(
            label="trailing-space-added",
            evidence_span=snippet + " ",
            haystack=base,
            expected_match=True,
            note="Agent-quoted span has one trailing space not in source",
        ))

    # Multi-line real text collapsed to one line by the agent when quoting.
    multiline = next((t for t in real_texts if t.count("\n") >= 2), None)
    if multiline:
        lines = multiline.split("\n")
        collapsed = " ".join(l.strip() for l in lines[:3] if l.strip())
        cases.append(MatchCase(
            label="multiline-collapsed",
            evidence_span=collapsed,
            haystack=multiline,
            expected_match=True,
            note="Agent quoted 3 real lines joined with spaces instead of newlines",
        ))

    # Paraphrase: MUST NOT match -- this is the negative control.
    cases.append(MatchCase(
        label="paraphrase-must-reject",
        evidence_span="the user said this approach was completely wrong and unacceptable",
        haystack="No, that's not right, please use the other method instead.",
        expected_match=False,
        note="Semantically similar but not a literal quote -- mechanical check must reject",
    ))

    # Fabricated quote that sounds plausible but was never said.
    cases.append(MatchCase(
        label="fabricated-quote-must-reject",
        evidence_span="I explicitly confirmed this design is correct",
        haystack="Sounds reasonable, let's try it and see what happens.",
        expected_match=False,
        note="Agent invents a stronger confirmation than what was actually said",
    ))

    # Quote spanning two separate message objects (agent conflates two turns).
    if len(real_texts) >= 2:
        span = real_texts[0][-30:] + real_texts[1][:30]
        cases.append(MatchCase(
            label="cross-message-boundary",
            evidence_span=span,
            haystack=real_texts[0] + real_texts[1],
            expected_match=True,  # true only if haystack is the concatenation of BOTH messages
            note=(
                "Only matches if the check searches across a multi-message "
                "window, not a single message in isolation -- tests whether "
                "the real implementation needs a multi-message haystack"
            ),
        ))
        cases.append(MatchCase(
            label="cross-message-boundary-single-message-haystack",
            evidence_span=span,
            haystack=real_texts[0],  # only ONE of the two real messages
            expected_match=False,
            note=(
                "Same spanning span, but checked against only message[0] -- "
                "this is what happens if the real implementation naively "
                "checks one message at a time instead of a window"
            ),
        ))

    return cases


def main() -> int:
    real_texts = load_real_transcript_texts()
    print(f"Loaded {len(real_texts)} real message text blocks from agent-transcripts/\n")

    cases = build_synthetic_edge_cases(real_texts)
    print(f"{len(cases)} test cases\n")

    results = {"exact": {"tp": 0, "tn": 0, "fp": 0, "fn": 0},
               "norm": {"tp": 0, "tn": 0, "fp": 0, "fn": 0}}

    for case in cases:
        exact = exact_substring_match(case.evidence_span, case.haystack)
        norm = whitespace_normalized_match(case.evidence_span, case.haystack)
        print(f"[{case.label}] expected={case.expected_match} exact={exact} norm={norm}")
        print(f"    note: {case.note}")
        for fn_name, got in (("exact", exact), ("norm", norm)):
            bucket = results[fn_name]
            if case.expected_match and got:
                bucket["tp"] += 1
            elif not case.expected_match and not got:
                bucket["tn"] += 1
            elif not case.expected_match and got:
                bucket["fp"] += 1
            else:
                bucket["fn"] += 1

    print("\n=== Summary ===")
    for fn_name, bucket in results.items():
        total = sum(bucket.values())
        correct = bucket["tp"] + bucket["tn"]
        print(f"{fn_name}: {correct}/{total} correct  "
              f"(false_accepts={bucket['fp']}, false_rejects={bucket['fn']})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
