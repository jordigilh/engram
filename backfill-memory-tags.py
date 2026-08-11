#!/usr/bin/env python3
"""One-off/rerunnable backfill: retag every pre-existing, untagged cursor-memory
document with its originating project (kubernaut/dcm/engram).

Why this exists: the 2026-07-27 fix to retain_windows() (see docs/FINDINGS.md)
tags every *new* cursor-memory retain with its project going forward, but
Hindsight's memory-curation API has no way to retag existing facts -- the only
supported write path for tags on already-retained content is
`PATCH /v1/default/banks/{bank_id}/documents/{document_id}` (tags on the
*document*, which per the API's own description propagate to its derived
memory units). This script walks the existing backlog and applies that PATCH
using the exact same transcript-path -> workspace -> project resolution logic
already used going forward (project_scope.resolve_project_label()).

Two categories of pre-existing document CANNOT be resolved this way and are
intentionally left untouched by this script:
  - `workspace == "empty-window"`: a blank Cursor session with no folder open
    at all. There is no project to attribute these to, ever -- not a gap.
  - No `transcript_id` in document_metadata at all (mostly `triage-rearrange`
    documents, whose upstream source document -- the one that actually had a
    transcript_id -- was already deleted by an earlier triage-memories.py
    pass; verified 2026-07-27 that 0/390 `original_doc` references still
    exist). These are handled by backfill-content-classified-tags.py instead,
    which classifies by fact *content* rather than transcript lineage.

Usage:
    python3 backfill-memory-tags.py [--dry-run] [--bank cursor-memory]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_scope

HINDSIGHT_URL = "http://localhost:8888"
TRANSCRIPTS_GLOB = os.path.expanduser(
    "~/.cursor/projects/*/agent-transcripts/**/*.jsonl"
)


def build_transcript_workspace_index() -> dict[str, str]:
    """Map transcript_id -> the Cursor workspace directory name that produced
    it, by scanning every transcript file currently on disk. Mirrors the
    lookup nightly-learn.py's project_for_transcript_path() does per-path,
    but indexed by transcript_id since documents only carry the id, not the
    original file path."""
    index: dict[str, str] = {}
    for path in glob.glob(TRANSCRIPTS_GLOB, recursive=True):
        tid = os.path.splitext(os.path.basename(path))[0]
        parts = path.split("/agent-transcripts/")
        if len(parts) == 2:
            index[tid] = os.path.basename(parts[0])
    return index


def fetch_all_documents(bank_id: str) -> list[dict]:
    docs: list[dict] = []
    offset = 0
    while True:
        req = Request(f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/documents?limit=200&offset={offset}")
        with urlopen(req, timeout=30) as resp:
            page = json.load(resp)
        docs.extend(page["items"])
        offset += len(page["items"])
        if offset >= page["total"] or not page["items"]:
            break
    return docs


def plan_retags(documents: list[dict], tid_to_workspace: dict[str, str]) -> list[dict]:
    """Pure planning function (no I/O): given the current document list and a
    transcript_id -> workspace index, return [{"document_id", "project"}, ...]
    for every document that (a) has no tags yet, (b) carries a resolvable
    transcript_id, and (c) that transcript's workspace maps to an onboarded
    project. Already-tagged documents and unresolvable ones (empty-window,
    no transcript_id, out-of-scope workspace) are silently skipped -- this is
    what makes the script idempotent and safe to re-run."""
    plan = []
    for doc in documents:
        if doc.get("tags"):
            continue
        meta = doc.get("document_metadata") or {}
        tid = meta.get("transcript_id")
        if not tid:
            continue
        workspace = tid_to_workspace.get(tid)
        if not workspace:
            continue
        label = project_scope.resolve_project_label(workspace)
        if not label:
            continue
        plan.append({"document_id": doc["id"], "project": label})
    return plan


def apply_retag(bank_id: str, document_id: str, project: str) -> tuple[bool, str | None]:
    payload = json.dumps({"tags": [project]}).encode()
    req = Request(
        f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/documents/{document_id}",
        data=payload, method="PATCH", headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.load(resp)
        return bool(result.get("success")), None
    except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
        return False, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Show the plan without writing")
    ap.add_argument("--bank", default="cursor-memory")
    args = ap.parse_args()

    print("Indexing transcript files on disk...")
    tid_to_workspace = build_transcript_workspace_index()
    print(f"  indexed {len(tid_to_workspace)} transcript files")

    print(f"Fetching documents from bank '{args.bank}'...")
    documents = fetch_all_documents(args.bank)
    print(f"  fetched {len(documents)} documents")

    plan = plan_retags(documents, tid_to_workspace)
    by_project: dict[str, int] = {}
    for item in plan:
        by_project[item["project"]] = by_project.get(item["project"], 0) + 1
    print(f"\nPlan: {len(plan)} documents to retag -> {by_project}")

    if args.dry_run:
        print("\n--dry-run: no writes performed.")
        return

    succeeded = 0
    failed = 0
    for i, item in enumerate(plan):
        ok, err = apply_retag(args.bank, item["document_id"], item["project"])
        if ok:
            succeeded += 1
        else:
            failed += 1
            print(f"  FAILED {item['document_id']}: {err}")
        if (i + 1) % 200 == 0:
            print(f"  ...{i + 1}/{len(plan)}")

    print(f"\nDone: {succeeded} retagged, {failed} failed, out of {len(plan)} planned.")


if __name__ == "__main__":
    main()
