#!/usr/bin/env python3
"""Reconcile paired matrix manifests against the exact baseline/candidate trees.

Use this when an earlier matrix runner version recorded arm-based index versions
or hashed only a partial source list. Run artifacts and scores are verified and
left byte-identical; only each arm's provenance manifest is corrected.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.replay_multilanguage_zvec import _current_index_version, _diff_digest, _git


LANGUAGES = ("go", "python", "rust", "typescript")
ENGINES = ("typescript", "rust")
ARMS = ("baseline", "candidate")
ARTIFACTS = ("raw-runs.json", "normalized-runs.json", "metrics-k10.json")


def reconcile(
    matrix: Path,
    baseline_worktree: Path,
    candidate_worktree: Path,
) -> list[Path]:
    baseline_worktree = baseline_worktree.resolve()
    candidate_worktree = candidate_worktree.resolve()
    touched = []
    for language in LANGUAGES:
        for engine in ENGINES:
            for arm in ARMS:
                directory = matrix / language / f"{engine}-{arm}"
                manifest_path = directory / "run-manifest.json"
                manifest: dict[str, Any] = json.loads(manifest_path.read_text())
                root = baseline_worktree if arm == "baseline" else candidate_worktree
                raw = json.loads((directory / "raw-runs.json").read_text())
                expected_version = _current_index_version(root, engine)
                expected_diff = _diff_digest(root, engine)
                expected_commit = _git(root, "rev-parse", "HEAD")
                if raw.get("binary_sha256") != manifest.get("binary_sha256"):
                    raise ValueError(f"binary hash disagrees with raw run: {directory}")
                for artifact in ARTIFACTS:
                    expected_hash = manifest.get("artifacts_sha256", {}).get(artifact)
                    if expected_hash is not None:
                        import hashlib

                        actual_hash = hashlib.sha256((directory / artifact).read_bytes()).hexdigest()
                        if actual_hash != expected_hash:
                            raise ValueError(f"artifact hash mismatch: {directory / artifact}")
                manifest["implementation_commit"] = expected_commit
                manifest["implementation_diff_sha256"] = expected_diff
                manifest["index_version"] = expected_version
                manifest["provenance_reconciliation"] = {
                    "method": "recomputed from the exact worktree; run outputs and metrics verified unchanged",
                    "diff_scope": "all tracked changes plus non-ignored untracked source/test files in the engine subtree",
                }
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
                touched.append(manifest_path)
    return touched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-dir", type=Path, required=True)
    parser.add_argument("--baseline-worktree", type=Path, required=True)
    parser.add_argument("--candidate-worktree", type=Path, required=True)
    args = parser.parse_args()
    for path in reconcile(args.matrix_dir, args.baseline_worktree, args.candidate_worktree):
        print(path)


if __name__ == "__main__":
    main()
