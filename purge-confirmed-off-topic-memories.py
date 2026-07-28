#!/usr/bin/env python3
"""One-off, re-runnable audit/cleanup tool: content-based sibling of
purge-out-of-scope-memories.py, for the untagged cursor-memory backlog that
script's transcript-path check can't reach.

purge-out-of-scope-memories.py only deletes documents it can CONFIRM are
out-of-scope via transcript_id -> workspace resolution; it explicitly leaves
documents with no resolvable transcript alone ("conservative: we can't
confirm out-of-scope, so we don't delete"). After the 2026-07-27 cursor-memory
retagging backfill (see docs/FINDINGS.md), 359 documents remain untagged --
sampling confirmed most are genuinely universal coding-hygiene lessons (this
bank's actual intended content), but a handful are confirmed off-topic
bleed-through from an entirely different, never-onboarded project (e.g. a
Django-migration/billing discussion, presumably koku/insights-onprem). This
script finds *only* that narrow, high-confidence subset using an LLM auditor
prompted to default to "not off-topic" whenever evidence is ambiguous --
mis-flagging a genuinely useful universal fact is worse than leaving an
ambiguous one alone.

Only ever operates on documents with NO project tag (kubernaut/dcm/engram
already-tagged documents are never touched -- if it was resolvable to one of
our onboarded projects, it isn't off-topic by definition).

Usage:
    ~/.hindsight/with-config-env.sh python3 purge-confirmed-off-topic-memories.py                        # dry run (default)
    ~/.hindsight/with-config-env.sh python3 purge-confirmed-off-topic-memories.py --execute               # actually deletes
    ~/.hindsight/with-config-env.sh python3 purge-confirmed-off-topic-memories.py --min-confidence 0.9

Every classification (flagged or not) is logged to
~/.hindsight/logs/off-topic-purge-audit.jsonl.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "spike"))
import classify
from contradiction_resolution import delete_document

# backfill-memory-tags.py has a hyphen, so it can't be `import`ed normally.
_spec = importlib.util.spec_from_file_location(
    "backfill_memory_tags", Path(__file__).resolve().parent / "backfill-memory-tags.py"
)
bmt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bmt)

HINDSIGHT_URL = "http://localhost:8888"
AUDIT_LOG = Path(os.path.expanduser("~/.hindsight/logs/off-topic-purge-audit.jsonl"))
DEFAULT_MIN_CONFIDENCE = 0.85  # higher bar than tagging (0.75) -- deletion is irreversible


def plan_purge_candidates(documents: list[dict]) -> list[dict]:
    """Pure planning function: every untagged document is a candidate for
    off-topic auditing. Already-tagged documents (resolved to kubernaut/dcm/
    engram, whether via transcript-path or content classification) are never
    touched -- by definition they're in-scope."""
    return [doc for doc in documents if not doc.get("tags")]


def should_purge(result: classify.OffTopicClassificationResult, min_confidence: float) -> bool:
    return result.off_topic and result.confidence >= min_confidence and not result.error


def fetch_document_text(bank_id: str, document_id: str) -> str:
    req = Request(f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/documents/{document_id}/chunks")
    with urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return "\n".join(item["chunk_text"] for item in data.get("items", []))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="Actually delete flagged documents (default: dry run)")
    ap.add_argument("--bank", default="cursor-memory")
    ap.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    args = ap.parse_args()

    print(f"Fetching documents from bank '{args.bank}'...")
    documents = bmt.fetch_all_documents(args.bank)
    candidates = plan_purge_candidates(documents)
    print(f"  {len(candidates)} untagged documents to audit for confirmed off-topic content\n")

    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    flagged = []

    with open(AUDIT_LOG, "a") as audit:
        for i, doc in enumerate(candidates):
            text = fetch_document_text(args.bank, doc["id"])
            result = classify.classify_off_topic_content(text)

            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "document_id": doc["id"],
                "text_preview": text[:200],
                "off_topic": result.off_topic,
                "identified_project": result.identified_project,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "error": result.error,
                "flagged": False,
                "deleted": False,
            }

            if should_purge(result, args.min_confidence):
                record["flagged"] = True
                flagged.append((doc, result))

            audit.write(json.dumps(record) + "\n")

            if (i + 1) % 50 == 0:
                print(f"  ...{i + 1}/{len(candidates)}")

    print(f"\nFlagged as confirmed off-topic (>= {args.min_confidence} confidence): {len(flagged)}")
    for doc, result in flagged[:20]:
        print(f"  [{result.identified_project}] {doc['id']} (conf={result.confidence:.2f}) — {result.reasoning}")
    if len(flagged) > 20:
        print(f"  ... and {len(flagged) - 20} more")

    if not flagged:
        print("\nNothing to do.")
        return

    if not args.execute:
        print(f"\nDry run only -- no documents were deleted. Re-run with --execute to delete.")
        print(f"Full audit trail: {AUDIT_LOG}")
        return

    print("\nDeleting...")
    deleted = 0
    with open(AUDIT_LOG, "a") as audit:
        for doc, result in flagged:
            ok = delete_document(args.bank, doc["id"])
            if ok:
                deleted += 1
            audit.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "document_id": doc["id"],
                "identified_project": result.identified_project,
                "confidence": result.confidence,
                "deleted": ok,
                "action": "execute",
            }) + "\n")

    print(f"Deleted {deleted}/{len(flagged)} confirmed off-topic documents.")
    print(f"Full audit trail: {AUDIT_LOG}")


if __name__ == "__main__":
    main()
