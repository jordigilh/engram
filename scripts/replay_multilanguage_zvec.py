#!/usr/bin/env python3
"""Build and replay paired TS/Rust zvec arms on four source-language fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import platform
import shutil
import subprocess
import sys
from typing import Any

from scripts.build_synthetic_qrels import _snapshot, build_qrels
from scripts.evaluate_semantic_search import evaluate
from scripts.replay_synthetic_semantic_search import _run_zvec, _unit_spans, _zvec_results


LANGUAGES = ("go", "python", "rust", "typescript")
MODEL_ID = "local/potion-code-16m-v2"
MODEL_SNAPSHOT = "e9d2a44ca6a05ac6685f3b23709ea57eb7352d5b"
INDEX_FLAGS = [
    "--mode", "direct", "--embedding", MODEL_ID,
    "--device", "cpu", "--hidden", "--no-ignore", "--max-filesize", "2M",
]
QUERY_FLAGS = ["--mode", "direct", "--refresh", "off", "--limit", "10", "--compact", "--preview", "full"]


class MatrixReplayError(RuntimeError):
    """Raised when a language/implementation arm cannot be reproduced safely."""


def _run(command: list[str], *, cwd: pathlib.Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise MatrixReplayError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stderr}"
        )
    return result.stdout


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: pathlib.Path, *args: str) -> str:
    return _run(["git", *args], cwd=root).strip()


def _diff_digest(root: pathlib.Path, paths: list[str]) -> str:
    encoded = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", *paths],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    return hashlib.sha256(encoded).hexdigest()


def _selected_source_paths(fixture: pathlib.Path, manifest: dict[str, Any]) -> tuple[str, list[str], int]:
    digest, paths, total_bytes = _snapshot(fixture, manifest)
    source = json.loads((fixture / "qrels.json").read_text())["source"]
    if (digest, len(paths), total_bytes) != (
        source["snapshot_sha256"], source["files"], source["bytes"]
    ):
        raise MatrixReplayError(f"fixture source/qrels mismatch: {fixture}")
    return digest, paths, total_bytes


def _prepare_root(fixture: pathlib.Path, work_root: pathlib.Path, paths: list[str]) -> None:
    if work_root.exists():
        raise MatrixReplayError(f"refusing to reuse workspace root: {work_root}")
    work_root.mkdir(parents=True)
    for relative in paths:
        source = fixture / relative
        destination = work_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _capture_arm(
    *,
    fixture: pathlib.Path,
    language: str,
    engine: str,
    arm: str,
    binary: pathlib.Path,
    work_parent: pathlib.Path,
    output_parent: pathlib.Path,
    model_cache: pathlib.Path,
    model_snapshot: str,
    implementation_commit: str,
    implementation_diff_sha256: str,
) -> dict[str, Any]:
    manifest = json.loads((fixture / "manifest.json").read_text())
    truth = json.loads((fixture / "truth.json").read_text())
    qrels = json.loads((fixture / "qrels.json").read_text())
    if build_qrels(fixture) != qrels:
        raise MatrixReplayError(f"qrels are not reproducible: {fixture}")
    digest, paths, total_bytes = _selected_source_paths(fixture, manifest)
    workspace_name = (
        f"qeval-{engine}-{arm}-{language}-{work_parent.name.rsplit('.', 1)[-1]}"
    )
    workspace_root = work_parent / workspace_name
    _prepare_root(fixture, workspace_root, paths)
    index_command = [str(binary), "--index", str(workspace_root), *INDEX_FLAGS,
                     "--model-cache", str(model_cache)]
    index_output = _run(index_command)
    verified_digest, verified_paths, verified_bytes = _snapshot(workspace_root, manifest)
    if (verified_digest, verified_paths, verified_bytes) != (digest, paths, total_bytes):
        raise MatrixReplayError(f"staged workspace differs from fixture: {workspace_root}")

    units = _unit_spans(fixture, manifest)
    rows = _run_zvec(binary, workspace_root, truth["queries"], model_cache)
    run_name = f"{engine}-{arm}"
    normalized = {
        "schema_version": 1,
        "suite_id": truth["suite_id"],
        "fixture_id": manifest["fixture_id"],
        "source": qrels["source"],
        "result_limit": 10,
        "runs": [{
            "backend": run_name,
            "queries": [
                {"id": row["id"], "results": _zvec_results(units, row["response"])}
                for row in rows
            ],
        }],
    }
    score = evaluate(qrels, normalized, cutoff=10)
    output = output_parent / language / run_name
    if output.exists():
        raise MatrixReplayError(f"refusing to overwrite replay output: {output}")
    output.mkdir(parents=True)
    raw = {
        "schema_version": 1,
        "suite_id": truth["suite_id"],
        "fixture_id": manifest["fixture_id"],
        "source": qrels["source"],
        "result_limit": 10,
        "backend": run_name,
        "binary_sha256": _sha256(binary),
        "workspace_root": str(workspace_root),
        "queries": rows,
    }
    run_manifest = {
        "protocol": "synthetic-workflow-discovery-v1/source-unit@10",
        "source_language": language,
        "source_digest": digest,
        "source_files": len(paths),
        "source_bytes": total_bytes,
        "fixture_manifest_sha256": _sha256(fixture / "manifest.json"),
        "fixture_truth_sha256": _sha256(fixture / "truth.json"),
        "qrels_sha256": _sha256(fixture / "qrels.json"),
        "engine": engine,
        "arm": arm,
        "implementation_commit": implementation_commit,
        "implementation_diff_sha256": implementation_diff_sha256,
        "binary_path": str(binary),
        "binary_sha256": raw["binary_sha256"],
        "index_version": {"typescript": 1 if arm == "baseline" else 2,
                           "rust": 2 if arm == "baseline" else 3}[engine],
        "embedding_model": MODEL_ID,
        "embedding_model_snapshot": model_snapshot,
        "device": "cpu",
        "index_flags": INDEX_FLAGS + ["--model-cache", str(model_cache)],
        "query_flags": QUERY_FLAGS + [
            "--model-cache", str(model_cache), "--device", "cpu",
            "--hybrid", "<verbatim truth.json query>",
        ],
        "raw_result_limit": 10,
        "normalized_cutoff": 10,
        "normalization": "source declaration spans from fixture manifest",
        "graph_sidecar": "absent",
        "os": platform.platform(),
        "index_output": index_output,
    }
    artifacts = (
        ("raw-runs.json", raw),
        ("normalized-runs.json", normalized),
        ("metrics-k10.json", score),
    )
    run_manifest["artifacts_sha256"] = {
        name: hashlib.sha256((json.dumps(value, indent=2) + "\n").encode()).hexdigest()
        for name, value in artifacts
    }
    for name, value in (*artifacts, ("run-manifest.json", run_manifest)):
        (output / name).write_text(json.dumps(value, indent=2) + "\n")
    return score["runs"][0]


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    fixture_root = args.fixtures.resolve()
    work_parent = args.work_dir.resolve()
    output_parent = args.output_dir.resolve()
    work_parent.mkdir(parents=True, exist_ok=True)
    output_parent.mkdir(parents=True, exist_ok=False)

    baseline_root = args.baseline_worktree.resolve()
    candidate_root = args.candidate_worktree.resolve()
    binaries = {
        ("typescript", "baseline"): args.baseline_typescript.resolve(),
        ("typescript", "candidate"): args.candidate_typescript.resolve(),
        ("rust", "baseline"): args.baseline_rust.resolve(),
        ("rust", "candidate"): args.candidate_rust.resolve(),
    }
    paths = {
        "typescript": [
            "src/engine/extraction/index.ts", "src/engine/extraction/vector-content.ts",
            "src/engine/storage/zvec.ts", "src/engine/types.ts",
        ],
        "rust": [
            "rust/crates/zg-engine/src/pipelines/indexing/pipeline.rs",
            "rust/crates/zg-engine/src/pipelines/indexing/service.rs",
            "rust/crates/zg-engine/src/workspace/manifest.rs",
            "rust/crates/zg-engine/src/workspace/mod.rs",
        ],
    }
    revisions = {
        "baseline": _git(baseline_root, "rev-parse", "HEAD"),
        "candidate": _git(candidate_root, "rev-parse", "HEAD"),
    }
    diff_hashes = {
        ("typescript", "baseline"): _diff_digest(baseline_root, paths["typescript"]),
        ("typescript", "candidate"): _diff_digest(candidate_root, paths["typescript"]),
        ("rust", "baseline"): _diff_digest(baseline_root, paths["rust"]),
        ("rust", "candidate"): _diff_digest(candidate_root, paths["rust"]),
    }
    model_cache = args.model_cache.resolve()
    summaries: dict[str, Any] = {language: {} for language in LANGUAGES}
    for language in LANGUAGES:
        fixture = fixture_root / f"{language}-workflow-discovery-v1"
        for engine in ("typescript", "rust"):
            for arm in ("baseline", "candidate"):
                summaries[language][f"{engine}-{arm}"] = _capture_arm(
                    fixture=fixture,
                    language=language,
                    engine=engine,
                    arm=arm,
                    binary=binaries[(engine, arm)],
                    work_parent=work_parent,
                    output_parent=output_parent,
                    model_cache=model_cache,
                    model_snapshot=args.model_snapshot,
                    implementation_commit=revisions[arm],
                    implementation_diff_sha256=diff_hashes[(engine, arm)],
                )
    comparison = {"schema_version": 1, "languages": {}}
    for language in LANGUAGES:
        comparison["languages"][language] = {}
        for engine in ("typescript", "rust"):
            baseline = summaries[language][f"{engine}-baseline"]
            candidate = summaries[language][f"{engine}-candidate"]
            baseline_queries = {row["id"]: row for row in baseline["per_query"]}
            candidate_queries = {row["id"]: row for row in candidate["per_query"]}
            comparison["languages"][language][engine] = {
                "baseline": baseline["overall"],
                "candidate": candidate["overall"],
                "delta": {
                    key: candidate["overall"][key] - baseline["overall"][key]
                    for key in baseline["overall"]
                },
                "per_query_delta": {
                    query_id: {
                        key: candidate_queries[query_id][key] - baseline_queries[query_id][key]
                        for key in baseline_queries[query_id]
                        if key not in {"id", "category"}
                    }
                    for query_id in baseline_queries
                },
            }
    comparison["run_policy"] = {
        "scores_are_language_specific": True,
        "cross_language_macro_average": "not computed",
        "baseline_candidate_pairs": "same implementation, fixture, qrels, model, device, query protocol",
    }
    (output_parent / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
    return comparison


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=pathlib.Path, required=True)
    parser.add_argument("--work-dir", type=pathlib.Path, required=True)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--baseline-worktree", type=pathlib.Path, required=True)
    parser.add_argument("--candidate-worktree", type=pathlib.Path, required=True)
    parser.add_argument("--baseline-typescript", type=pathlib.Path, required=True)
    parser.add_argument("--candidate-typescript", type=pathlib.Path, required=True)
    parser.add_argument("--baseline-rust", type=pathlib.Path, required=True)
    parser.add_argument("--candidate-rust", type=pathlib.Path, required=True)
    parser.add_argument("--model-cache", type=pathlib.Path, required=True)
    parser.add_argument("--model-snapshot", default=MODEL_SNAPSHOT)
    args = parser.parse_args()
    try:
        comparison = run_matrix(args)
    except (OSError, KeyError, ValueError, MatrixReplayError, subprocess.SubprocessError) as error:
        print(f"multilanguage replay error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    for language, rows in comparison["languages"].items():
        for engine, result in rows.items():
            delta = result["delta"]
            print(
                f"{language:10s} {engine:10s} "
                f"nDCG {delta['ndcg@10']:+.4f} "
                f"MRR {delta['mrr@10']:+.4f} "
                f"Recall {delta['recall@10']:+.4f} "
                f"Precision {delta['precision@10']:+.4f}"
            )


if __name__ == "__main__":
    main()
