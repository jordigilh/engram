#!/usr/bin/env python3
"""One-off backfill: tag cursor-memory documents that have NO transcript
lineage to trace (mostly `triage-rearrange` documents whose upstream source
document -- the one that actually carried a transcript_id -- was already
deleted by an earlier triage-memories.py pass; see docs/FINDINGS.md
2026-07-27) using content-based LLM classification instead of the
transcript-path resolution backfill-memory-tags.py uses for the rest of the
backlog.

This is intentionally a *lower-confidence, best-effort* pass: reading a
fact's own text for project signals is far weaker evidence than the
transcript-path lineage used elsewhere, so results below --min-confidence
(default 0.75) are left untagged rather than force a guess -- a wrong tag
would incorrectly exclude/include a fact from the wrong project's recall,
which is worse than leaving it unscoped. Every classification (applied or
not) is appended to an audit log for manual review.

Usage:
    python3 backfill-content-classified-tags.py [--dry-run] [--min-confidence 0.75]

Must run with Vertex AI env vars populated (VERTEXAI_PROJECT/LOCATION,
GOOGLE_APPLICATION_CREDENTIALS) -- e.g. via with-config-env.sh, same as every
other LLM-calling script in this repo:
    ~/.hindsight/with-config-env.sh python3 backfill-content-classified-tags.py
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "spike"))
import classify

# backfill-memory-tags.py has a hyphen, so it can't be `import`ed normally.
_spec = importlib.util.spec_from_file_location(
    "backfill_memory_tags", Path(__file__).resolve().parent / "backfill-memory-tags.py"
)
bmt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bmt)

HINDSIGHT_URL = "http://localhost:8888"
AUDIT_LOG = Path(os.path.expanduser("~/.hindsight/logs/content-classification-audit.jsonl"))


def plan_content_targets(documents: list[dict]) -> list[dict]:
    """Pure planning function: documents with no tags AND no transcript_id
    at all -- i.e. the ones plan_retags() in backfill-memory-tags.py can
    never resolve, because there's no lineage left to trace."""
    targets = []
    for doc in documents:
        if doc.get("tags"):
            continue
        meta = doc.get("document_metadata") or {}
        if meta.get("transcript_id"):
            continue
        targets.append(doc)
    return targets


def fetch_document_text(bank_id: str, document_id: str) -> str:
    req = Request(f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/documents/{document_id}/chunks")
    with urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return "\n".join(item["chunk_text"] for item in data.get("items", []))


def should_apply_tag(result: classify.ProjectClassificationResult, min_confidence: float) -> bool:
    return result.project is not None and result.confidence >= min_confidence and not result.error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--bank", default="cursor-memory")
    ap.add_argument("--min-confidence", type=float, default=0.75)
    args = ap.parse_args()

    print(f"Fetching documents from bank '{args.bank}'...")
    documents = bmt.fetch_all_documents(args.bank)
    targets = plan_content_targets(documents)
    print(f"  {len(targets)} documents with no transcript lineage to classify by content")

    if args.dry_run:
        print("--dry-run: showing first 5 targets only, no LLM calls made.")
        for doc in targets[:5]:
            print(" ", doc["id"], (doc.get("document_metadata") or {}).get("source"))
        return

    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    applied = {"kubernaut": 0, "dcm": 0, "engram": 0}
    left_untagged = 0
    errors = 0

    with open(AUDIT_LOG, "a") as audit:
        for i, doc in enumerate(targets):
            text = fetch_document_text(args.bank, doc["id"])
            result = classify.classify_project_from_content(text)

            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "document_id": doc["id"],
                "text_preview": text[:200],
                "classified_project": result.project,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "error": result.error,
                "applied": False,
            }

            if result.error:
                errors += 1
            elif should_apply_tag(result, args.min_confidence):
                ok, err = bmt.apply_retag(args.bank, doc["id"], result.project)
                if ok:
                    applied[result.project] += 1
                    record["applied"] = True
                else:
                    errors += 1
                    record["error"] = err
            else:
                left_untagged += 1

            audit.write(json.dumps(record) + "\n")

            if (i + 1) % 50 == 0:
                print(f"  ...{i + 1}/{len(targets)}")

    print(f"\nDone: applied={applied}, left_untagged={left_untagged}, errors={errors}")
    print(f"Full audit trail: {AUDIT_LOG}")


if __name__ == "__main__":
    main()
