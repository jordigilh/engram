#!/usr/bin/env python3
"""One-off backfill: tag cursor-memory documents that backfill-memory-tags.py's
transcript-path resolution (plan_retags()) could NOT resolve, using
content-based LLM classification instead.

Two distinct sub-populations land here, for different reasons:
  - No `transcript_id` at all (mostly `triage-rearrange` documents whose
    upstream source document -- the one that actually carried a
    transcript_id -- was already deleted by an earlier triage-memories.py
    pass; confirmed 0/390 `original_doc` references still resolve).
  - Transcript_id present, but its workspace resolved to `"empty-window"`
    (no Cursor folder open at all) or an out-of-scope workspace. Despite the
    name, sampling 12 random "empty-window" documents on 2026-07-27 found
    every single one full of clear per-project signal (kubernaut PR/issue
    URLs, "kubernaut-operator", "SP CRD", GA-gate checklists, etc) -- the
    "no folder open" workspace label reflects a Cursor UI/session-recording
    quirk, not an actual absence of project context. These are just as
    viable for content classification as the no-transcript_id bucket, so
    both are handled by one pass here rather than writing them off as
    permanently unattributable.

This is intentionally a *lower-confidence, best-effort* pass: reading a
fact's own text for project signals is weaker evidence than transcript-path
lineage, so results below --min-confidence (default 0.75) are left untagged
rather than force a guess -- a wrong tag would incorrectly exclude/include a
fact from the wrong project's recall, which is worse than leaving it
unscoped. Every classification (applied or not) is appended to an audit log
for manual review.

Usage:
    python3 backfill-content-classified-tags.py [--dry-run] [--min-confidence 0.75]

Must run with Vertex AI env vars populated (VERTEXAI_PROJECT/LOCATION,
GOOGLE_APPLICATION_CREDENTIALS) -- e.g. via with-config-env.sh, same as every
other LLM-calling script in this repo:
    ~/.hindsight/with-config-env.sh python3 backfill-content-classified-tags.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

# This file is part of the engram.maintenance package
# (src/engram/maintenance/), three directories below the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from engram import classify  # noqa: E402
from engram.maintenance import backfill_memory_tags as bmt  # noqa: E402

HINDSIGHT_URL = "http://localhost:8888"
AUDIT_LOG = Path(os.path.expanduser("~/.hindsight/logs/content-classification-audit.jsonl"))


def plan_content_targets(documents: list[dict], resolvable_ids: set[str]) -> list[dict]:
    """Pure planning function: every untagged document NOT in
    `resolvable_ids` (the set backfill-memory-tags.py's plan_retags() can
    already resolve via transcript-path lineage). Covers both the
    no-transcript_id bucket and the "empty-window"/out-of-scope-workspace
    bucket -- see module docstring for why both are worth attempting."""
    targets = []
    for doc in documents:
        if doc.get("tags"):
            continue
        if doc["id"] in resolvable_ids:
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

    print("Indexing transcript files on disk...")
    tid_to_workspace = bmt.build_transcript_workspace_index()

    print(f"Fetching documents from bank '{args.bank}'...")
    documents = bmt.fetch_all_documents(args.bank)
    resolvable_ids = {item["document_id"] for item in bmt.plan_retags(documents, tid_to_workspace)}
    targets = plan_content_targets(documents, resolvable_ids)
    print(f"  {len(targets)} documents not resolvable by transcript-path -- classifying by content")

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
