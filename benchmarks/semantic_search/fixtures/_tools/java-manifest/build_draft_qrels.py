#!/usr/bin/env python3
"""Build a DRAFT qrels file from source-authored truth for the Java fixture.

Unlike scripts/build_synthetic_qrels.py this tool keeps
``judgment_status`` at ``draft`` because the Java lane has not been reviewed by
a human adjudicator yet. The scorer refuses draft labels, which is intentional.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import pathlib
from typing import Any

DRAFT_RATIONALE = (
    "Source-authored fixture negative: this unit is not an answer site for the query."
)


def selected_files(root: pathlib.Path, include: list[str], exclude: list[str]) -> list[str]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if not any(fnmatch.fnmatchcase(relative, pattern) for pattern in include):
            continue
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in exclude):
            continue
        files.append(relative)
    return sorted(files)


def snapshot(root: pathlib.Path, include: list[str], exclude: list[str]) -> tuple[str, int]:
    """sha256 over sorted ``<path>\\t<sha256-of-file-bytes>`` lines, joined by LF."""
    files = selected_files(root, include, exclude)
    lines = []
    total = 0
    for relative in files:
        content = (root / relative).read_bytes()
        total += len(content)
        lines.append(f"{relative}\t{hashlib.sha256(content).hexdigest()}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest(), total


def build(fixture: pathlib.Path) -> dict[str, Any]:
    manifest = json.loads((fixture / "manifest.json").read_text())
    truth = json.loads((fixture / "truth.json").read_text())
    units = manifest["units"]
    unit_ids = [unit["unit_id"] for unit in units]
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("manifest unit IDs are not unique")

    source = dict(manifest["source"])
    digest, total_bytes = snapshot(fixture, source["include"], source["exclude"])
    source["snapshot_sha256"] = digest
    source["bytes"] = total_bytes

    queries = []
    for query in truth["queries"]:
        authored = {}
        for row in query["judgments"]:
            unit_id = row["unit_id"]
            if unit_id not in unit_ids:
                raise ValueError(f"{query['id']}: unknown unit {unit_id}")
            if unit_id in authored:
                raise ValueError(f"{query['id']}: duplicate judgment {unit_id}")
            authored[unit_id] = row
        judgments = []
        for unit_id in unit_ids:
            row = authored.get(unit_id)
            judgments.append({
                "unit_id": unit_id,
                "grade": row["grade"] if row else 0,
                "rationale": row["rationale"] if row else DRAFT_RATIONALE,
            })
        queries.append({
            "id": query["id"],
            "query": query["query"],
            "category": query["category"],
            "judgments": judgments,
        })

    return {
        "schema_version": 1,
        "suite_id": truth["suite_id"],
        "fixture_id": manifest["fixture_id"],
        "judgment_status": "draft",
        "candidate_pool_complete": True,
        "source_truth": True,
        "source": source,
        "unit_count": len(unit_ids),
        "queries": queries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    qrels = build(args.fixture)
    args.output.write_text(json.dumps(qrels, indent=2) + "\n")
    print(f"wrote {args.output}: {qrels['unit_count']} units, "
          f"{len(qrels['queries'])} queries, status={qrels['judgment_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
