#!/usr/bin/env python3
"""One-off, re-runnable audit/cleanup tool: finds and deletes cursor-memory
documents that trace back to a transcript from a Cursor workspace NOT in
project_scope.ALLOWED_WORKSPACE_PREFIXES.

Written 2026-07-13 after discovering nightly-learn.py/cocoindex-flows.py had
no project filter on the retain path -- see docs/FINDINGS.md. Confirmed 139
of 444 transcript-attributable cursor-memory documents (31%) came from
out-of-scope workspaces (insights-onprem/koku, redhat-developer-rhdh-plugins,
blank "no folder open" sessions, etc.) before this was fixed.

Only touches documents with a resolvable document_metadata.transcript_id.
Documents without one (curated/"triage-rearrange" facts predating the
transcript pipeline) are left untouched -- different provenance, not part of
this bug. Documents whose transcript_id can't be resolved to any on-disk
workspace (transcript file moved/deleted) are also left untouched --
conservative: we can't confirm out-of-scope, so we don't delete.

Usage:
    python3 purge-out-of-scope-memories.py            # dry run (default)
    python3 purge-out-of-scope-memories.py --execute   # actually deletes

Every deletion is logged to ~/.hindsight/logs/scope-purge-<timestamp>.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import project_scope
from contradiction_resolution import delete_document

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")
BANK_ID = "cursor-memory"
PROJECTS_ROOT = Path(os.path.expanduser("~/.cursor/projects"))
LOG_DIR = Path.home() / ".hindsight" / "logs"


def build_transcript_project_map() -> dict[str, str]:
    """transcript_id (filename stem) -> Cursor workspace directory name."""
    mapping: dict[str, str] = {}
    if not PROJECTS_ROOT.exists():
        return mapping
    for proj_dir in PROJECTS_ROOT.iterdir():
        if not proj_dir.is_dir():
            continue
        at_dir = proj_dir / "agent-transcripts"
        if not at_dir.exists():
            continue
        for f in at_dir.rglob("*.jsonl"):
            mapping[f.stem] = proj_dir.name
    return mapping


def fetch_all_documents(bank_id: str) -> list[dict]:
    all_docs: list[dict] = []
    offset = 0
    while True:
        req = Request(f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/documents?limit=100&offset={offset}")
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        items = data.get("items", data.get("documents", []))
        if not items:
            break
        all_docs.extend(items)
        offset += len(items)
        if len(items) < 100:
            break
    return all_docs


def classify(all_docs: list[dict], tid_to_project: dict[str, str]) -> dict[str, list[dict]]:
    """Bucket documents into: to_delete, no_transcript_id, unresolved."""
    buckets: dict[str, list[dict]] = {"to_delete": [], "no_transcript_id": [], "unresolved": []}
    for doc in all_docs:
        tid = (doc.get("document_metadata") or {}).get("transcript_id")
        if not tid:
            buckets["no_transcript_id"].append(doc)
            continue
        proj = tid_to_project.get(tid)
        if proj is None:
            buckets["unresolved"].append(doc)
            continue
        doc["_resolved_project"] = proj
        if project_scope.is_allowed_workspace(proj):
            continue
        buckets["to_delete"].append(doc)
    return buckets


def log_purge(entries: list[dict]) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = LOG_DIR / f"scope-purge-{ts}.jsonl"
    with open(path, "a") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually delete out-of-scope documents (default: dry run, print only)",
    )
    args = parser.parse_args()

    print(f"Building transcript -> project map from {PROJECTS_ROOT} ...")
    tid_to_project = build_transcript_project_map()
    print(f"  Indexed {len(tid_to_project)} transcript files.\n")

    print(f"Fetching all documents from bank={BANK_ID} ...")
    all_docs = fetch_all_documents(BANK_ID)
    print(f"  {len(all_docs)} total documents.\n")

    buckets = classify(all_docs, tid_to_project)
    to_delete = buckets["to_delete"]

    by_project: dict[str, int] = Counter(d["_resolved_project"] for d in to_delete)
    print(f"Out-of-scope documents to {'DELETE' if args.execute else 'delete (dry run)'}: {len(to_delete)}")
    for proj, count in sorted(by_project.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4d}  {proj}")
    print()
    print(f"Left untouched -- no transcript_id (curated/triage facts): {len(buckets['no_transcript_id'])}")
    print(f"Left untouched -- unresolved transcript_id (file not found): {len(buckets['unresolved'])}")
    print()

    if not to_delete:
        print("Nothing to do.")
        return 0

    if not args.execute:
        print("Dry run only -- no documents were deleted. Re-run with --execute to delete.")
        print("\nSample of documents that would be deleted:")
        for doc in to_delete[:10]:
            print(f"  [{doc['_resolved_project']}] {doc['id']}")
        if len(to_delete) > 10:
            print(f"  ... and {len(to_delete) - 10} more")
        return 0

    print("Deleting...")
    audit_entries = []
    deleted = 0
    for doc in to_delete:
        ok = delete_document(BANK_ID, doc["id"])
        audit_entries.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "document_id": doc["id"],
            "project": doc["_resolved_project"],
            "transcript_id": (doc.get("document_metadata") or {}).get("transcript_id"),
            "deleted": ok,
        })
        if ok:
            deleted += 1

    log_path = log_purge(audit_entries)
    print(f"Deleted {deleted}/{len(to_delete)} documents. Audit log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
