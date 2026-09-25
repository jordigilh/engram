#!/usr/bin/env python3
"""Capture a single zvec-grep synthetic run against the frozen source truth.

The three-backend replay requires CocoIndex and Sense to be live. This focused
runner keeps zvec ablations comparable without replaying those reference arms.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.build_synthetic_qrels import _snapshot, build_qrels
from scripts.evaluate_semantic_search import evaluate
from scripts.replay_synthetic_semantic_search import _run_zvec, _unit_spans, _zvec_results


def replay(
    fixture: Path, root: Path, binary: Path, model_cache: Path, backend: str
) -> tuple[dict, dict, dict]:
    fixture = fixture.resolve()
    root = root.resolve()
    binary = binary.resolve()
    manifest = json.loads((fixture / "manifest.json").read_text())
    truth = json.loads((fixture / "truth.json").read_text())
    qrels = json.loads((fixture / "qrels.json").read_text())
    if build_qrels(fixture) != qrels:
        raise ValueError("fixture source, manifest, truth and saved qrels disagree")
    source_digest, paths, source_bytes = _snapshot(root, manifest)
    source = qrels["source"]
    if (source_digest, len(paths), source_bytes) != (
        source["snapshot_sha256"], source["files"], source["bytes"]
    ):
        raise ValueError("indexed source root differs from the frozen fixture")
    queries = truth["queries"]
    units = _unit_spans(fixture, manifest)
    rows = _run_zvec(binary, root, queries, model_cache)
    with binary.open("rb") as binary_file:
        binary_sha256 = hashlib.file_digest(binary_file, "sha256").hexdigest()
    normalized = {
        "schema_version": 1,
        "suite_id": truth["suite_id"],
        "fixture_id": manifest["fixture_id"],
        "source": source,
        "result_limit": 10,
        "runs": [{
            "backend": backend,
            "queries": [
                {"id": row["id"], "results": _zvec_results(units, row["response"])}
                for row in rows
            ],
        }],
    }
    raw = {
        "schema_version": 1,
        "suite_id": truth["suite_id"],
        "fixture_id": manifest["fixture_id"],
        "source": source,
        "result_limit": 10,
        "backend": backend,
        "binary_sha256": binary_sha256,
        "root": str(root),
        "model_cache": str(model_cache.resolve()),
        "queries": rows,
    }
    return raw, normalized, evaluate(qrels, normalized, cutoff=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error(f"output directory already exists: {args.output_dir}")
    raw, normalized, metrics = replay(
        args.fixture, args.root, args.binary, args.model_cache, args.backend
    )
    args.output_dir.mkdir(parents=True)
    for name, contents in (
        ("raw-runs.json", raw),
        ("normalized-runs.json", normalized),
        ("metrics-k10.json", metrics),
    ):
        (args.output_dir / name).write_text(json.dumps(contents, indent=2) + "\n")


if __name__ == "__main__":
    main()
