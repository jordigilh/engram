#!/usr/bin/env python3
"""Build complete qrels from source-authored synthetic fixture truth."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
from typing import Any


class FixtureError(ValueError):
    """Raised when a fixture truth file is inconsistent with its manifest."""


def _snapshot(root: pathlib.Path, manifest: dict[str, Any]) -> tuple[str, list[str], int]:
    files = []
    for path in root.rglob("*.go"):
        relative = path.relative_to(root).as_posix()
        if relative.endswith("_test.go"):
            continue
        files.append(relative)
    files.sort()

    digest = hashlib.sha256()
    total_bytes = 0
    for relative in files:
        content = (root / relative).read_bytes()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
        total_bytes += len(content)
    return digest.hexdigest(), files, total_bytes


def build_qrels(fixture_dir: pathlib.Path) -> dict[str, Any]:
    manifest = json.loads((fixture_dir / "manifest.json").read_text())
    truth = json.loads((fixture_dir / "truth.json").read_text())
    units = manifest.get("units")
    queries = truth.get("queries")
    if not isinstance(units, list) or not units:
        raise FixtureError("manifest must contain a non-empty units list")
    if not isinstance(queries, list) or not queries:
        raise FixtureError("truth must contain a non-empty queries list")

    unit_ids = [unit.get("unit_id") for unit in units]
    if any(not unit_id for unit_id in unit_ids) or len(unit_ids) != len(set(unit_ids)):
        raise FixtureError("manifest unit IDs must be present and unique")
    digest, files, total_bytes = _snapshot(fixture_dir, manifest)

    output_queries = []
    for query in queries:
        query_id = query.get("id")
        target_rows = query.get("judgments")
        if not query_id or not isinstance(target_rows, list) or not target_rows:
            raise FixtureError(f"query {query_id!r} has no source-authored judgments")
        targets: dict[str, dict[str, Any]] = {}
        for target in target_rows:
            unit_id = target.get("unit_id")
            grade = target.get("grade")
            if unit_id not in unit_ids or unit_id in targets:
                raise FixtureError(f"query {query_id!r} has an unknown or duplicate target: {unit_id!r}")
            if not isinstance(grade, int) or isinstance(grade, bool) or not 0 <= grade <= 3:
                raise FixtureError(f"query {query_id!r}, unit {unit_id!r}: invalid grade")
            targets[unit_id] = target

        judgments = []
        for unit_id in unit_ids:
            target = targets.get(unit_id)
            judgments.append({
                "unit_id": unit_id,
                "grade": target["grade"] if target else 0,
                "rationale": (
                    target["rationale"]
                    if target
                    else "Source-authored fixture negative: this unit is not an answer site for the query."
                ),
            })
        output_queries.append({
            "id": query_id,
            "query": query["query"],
            "category": query["category"],
            "judgments": judgments,
        })

    source = dict(manifest["source"])
    source.update({"snapshot_sha256": digest, "files": len(files), "bytes": total_bytes})
    return {
        "schema_version": 1,
        "suite_id": truth["suite_id"],
        "fixture_id": manifest["fixture_id"],
        "judgment_status": "adjudicated",
        "candidate_pool_complete": True,
        "source_truth": True,
        "source": source,
        "unit_count": len(unit_ids),
        "queries": output_queries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    qrels = build_qrels(args.fixture)
    args.output.write_text(json.dumps(qrels, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
