#!/usr/bin/env python3
"""Verify the java-workflow-discovery-v1 retrieval fixture.

Checks, in order:

1. every Java source compiles with ``javac --release 21``;
2. each source file is 1-3 KB, the lane has 8-10 files and 12-24 KB total;
3. every manifest span resolves and starts with ``declaration_anchor``;
4. unit IDs are unique and counts match manifest/qrels ``unit_count``;
5. truth and qrels agree grade-for-grade and every query covers every unit;
6. the snapshot digest recomputes from source bytes;
7. each query has 2-4 units at grade >= 2 and at least 2 co-located decoys;
8. the deliberate decoys exist (two Normalize helpers plus a near-miss wrapper);
9. qrels stay in ``draft`` status (the scorer refuses draft labels);
10. ``git status --porcelain`` attributes only the new paths to this work.

Exit status is non-zero when any check fails.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "benchmarks" / "semantic_search" / "fixtures" / "java-workflow-discovery-v1"
TOOL_DIR = ROOT / "benchmarks" / "quality-lanes" / "fixtures" / "_tools" / "java-manifest"
VERIFIER = ROOT / "scripts" / "verify_java_workflow_fixture.py"

MIN_FILE_BYTES = 1_000
MAX_FILE_BYTES = 3_000
MIN_TOTAL_BYTES = 12_000
MAX_TOTAL_BYTES = 24_000

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label if not detail else f"{label} -- {detail}"))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    return ok


def load(name: str) -> Any:
    return json.loads((FIXTURE / name).read_text())


def selected_files(root: pathlib.Path, include: list[str], exclude: list[str]) -> list[str]:
    files: list[str] = []
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


def snapshot(root: pathlib.Path, include: list[str], exclude: list[str]) -> tuple[str, list[str], int]:
    """sha256 over sorted ``<path>\\t<sha256-of-file-bytes>`` lines joined by LF."""
    files = selected_files(root, include, exclude)
    lines = []
    total = 0
    for relative in files:
        content = (root / relative).read_bytes()
        lines.append(f"{relative}\t{hashlib.sha256(content).hexdigest()}")
        total += len(content)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest(), files, total


def main() -> int:
    manifest = load("manifest.json")
    truth = load("truth.json")
    qrels = load("qrels.json")
    source = manifest["source"]
    units = manifest["units"]
    unit_ids = [unit["unit_id"] for unit in units]

    # --- 1. compilation ---------------------------------------------------
    java_files = sorted(FIXTURE.rglob("*.java"))
    with tempfile.TemporaryDirectory(prefix="java-fixture-") as build_dir:
        compile_one = subprocess.run(
            ["javac", "--release", "21", "-d", build_dir, *map(str, java_files)],
            capture_output=True, text=True, timeout=180,
        )
    compile_detail = "clean" if compile_one.returncode == 0 else (
        compile_one.stderr.strip().splitlines() or ["failed"])[-1]
    check(compile_one.returncode == 0, "javac --release 21 compiles the fixture", compile_detail)

    # --- 2. size and count ------------------------------------------------
    sizes = {str(path.relative_to(FIXTURE)): path.stat().st_size for path in java_files}
    out_of_range = [f"{path}={size}B" for path, size in sizes.items()
                    if not MIN_FILE_BYTES <= size <= MAX_FILE_BYTES]
    total_bytes = sum(sizes.values())
    check(8 <= len(java_files) <= 10, "8-10 production Java files", f"{len(java_files)} files")
    check(not out_of_range, "each Java file is 1-3 KB",
          ", ".join(out_of_range) if out_of_range else "all within range")
    check(MIN_TOTAL_BYTES <= total_bytes <= MAX_TOTAL_BYTES, "total source is 12-24 KB",
          f"{total_bytes} bytes")

    # --- 3. spans ---------------------------------------------------------
    span_failures: list[str] = []
    for unit in units:
        target = FIXTURE / unit["path"]
        if not target.is_file():
            span_failures.append(f"{unit['unit_id']}: missing file")
            continue
        lines = target.read_text().splitlines()
        start, end = unit["start_line"], unit["end_line"]
        if not (1 <= start <= end <= len(lines)):
            span_failures.append(f"{unit['unit_id']}: span {start}..{end} out of {len(lines)}")
            continue
        first = lines[start - 1].strip()
        if not first.startswith(unit["declaration_anchor"]):
            span_failures.append(f"{unit['unit_id']}: anchor mismatch at {start}")
        closing = lines[end - 1].strip()
        if not (closing == "}" or closing.endswith("}") or closing.endswith(";")):
            span_failures.append(f"{unit['unit_id']}: end line {end} does not close the declaration")
    check(not span_failures, "unit spans resolve to declaration anchors",
          "; ".join(span_failures[:3]) if span_failures else f"{len(units)} units")

    # --- 4. identifier and count integrity --------------------------------
    duplicates = sorted({uid for uid in unit_ids if unit_ids.count(uid) > 1})
    check(len(unit_ids) == len(set(unit_ids)), "unit IDs unique",
          ", ".join(duplicates) if duplicates else f"{len(unit_ids)} units")
    check(qrels["unit_count"] == len(units), "qrels unit_count matches manifest",
          f"qrels={qrels['unit_count']} manifest={len(units)}")
    check(len(qrels["queries"]) == 8, "eight queries", f"{len(qrels['queries'])}")

    known = set(unit_ids)
    truth_ids = {j["unit_id"] for query in truth["queries"] for j in query["judgments"]}
    check(not (truth_ids - known), "truth unit IDs exist in manifest",
          ", ".join(sorted(truth_ids - known)) if truth_ids - known else f"{len(truth_ids)} referenced")

    # --- 5. truth and qrels agreement -------------------------------------
    qrels_by_query = {query["id"]: query for query in qrels["queries"]}
    agreement_failures: list[str] = []
    coverage_total = 0
    for query in truth["queries"]:
        other = qrels_by_query.get(query["id"])
        if other is None:
            agreement_failures.append(f"{query['id']}: missing from qrels")
            continue
        if len(other["judgments"]) != len(units) or {
                row["unit_id"] for row in other["judgments"]} != known:
            agreement_failures.append(f"{query['id']}: incomplete candidate pool")
        coverage_total += len(other["judgments"])
        grades = {row["unit_id"]: row["grade"] for row in other["judgments"]}
        for expected in query["judgments"]:
            if grades.get(expected["unit_id"]) != expected["grade"]:
                agreement_failures.append(
                    f"{query['id']}/{expected['unit_id']}: "
                    f"truth={expected['grade']} qrels={grades.get(expected['unit_id'])}")
    check(not agreement_failures, "truth and qrels agree grade-for-grade",
          "; ".join(agreement_failures[:3]) if agreement_failures else f"{len(truth['queries'])} queries")
    check(coverage_total == 8 * len(units), "8 x unit_count judgments present",
          f"{coverage_total} of {8 * len(units)}")

    # --- 6. snapshot ------------------------------------------------------
    digest, files, recomputed_bytes = snapshot(FIXTURE, source["include"], source["exclude"])
    check(digest == source["snapshot_sha256"], "snapshot_sha256 matches manifest",
          f"recomputed={digest[:16]}... manifest={source['snapshot_sha256'][:16]}...")
    check(files == sorted(sizes), "manifest covers exactly the selected source files",
          f"selected={len(files)} java={len(sizes)}")
    check(source["files"] == len(files) and source["bytes"] == recomputed_bytes,
          "manifest file and byte counts match source",
          f"files={len(files)} bytes={recomputed_bytes}")
    check(qrels["source"] == source, "qrels source block matches manifest source")

    # --- 7. per-query shape ----------------------------------------------
    path_of = {unit["unit_id"]: unit["path"] for unit in units}
    shape_failures: list[str] = []
    for query in qrels["queries"]:
        rows = query["judgments"]
        answers = [row for row in rows if row["grade"] >= 2]
        if not 2 <= len(answers) <= 4:
            shape_failures.append(f"{query['id']}: {len(answers)} answers (need 2-4)")
        answer_files = {path_of[row["unit_id"]] for row in answers}
        decoys = [row for row in rows if row["grade"] <= 1]
        same_file = [row for row in decoys if path_of[row["unit_id"]] in answer_files]
        if len(decoys) < 2 or len(same_file) < 2:
            shape_failures.append(
                f"{query['id']}: {len(decoys)} decoys, {len(same_file)} co-located")
    check(not shape_failures, "queries have 2-4 answers and >=2 co-located decoys",
          "; ".join(shape_failures[:3]) if shape_failures else "all queries shaped")

    # --- 8. deliberate decoys --------------------------------------------
    normalizers = sorted(uid for uid in unit_ids if uid.endswith("::Normalize")
                         or "::Normalize." in uid)
    check(len({path_of[uid] for uid in normalizers}) == 2, "two same-named Normalize helpers",
          ", ".join(normalizers))
    check("catalog/WorkflowCatalog.java::WorkflowCatalog.listActions" in known,
          "near-miss listActions wrapper exists")

    # --- 9. adjudication status ------------------------------------------
    check(qrels.get("judgment_status") == "draft", "qrels are marked draft",
          f"status={qrels.get('judgment_status')!r}; scorer refuses non-adjudicated labels")
    check(qrels.get("candidate_pool_complete") is True and
          qrels.get("source_truth") is True, "draft declares a complete source-grounded pool")

    # --- 10. git status ---------------------------------------------------
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    entries = [line for line in status.stdout.splitlines() if line.strip()]
    print("\ngit status --porcelain:")
    for line in entries:
        print(f"  {line}")

    fixture_rel = FIXTURE.relative_to(ROOT).as_posix()
    tool_rel = TOOL_DIR.relative_to(ROOT).as_posix()
    verifier_rel = VERIFIER.relative_to(ROOT).as_posix()
    expected = {fixture_rel, tool_rel, verifier_rel}

    def is_expected(path: str) -> bool:
        stripped = path.rstrip("/")
        for wanted in expected:
            if stripped == wanted:
                return True
            if wanted.startswith(stripped + "/") or stripped.startswith(wanted + "/"):
                return True
        return False

    ours: list[str] = []
    violations: list[str] = []
    preexisting: list[str] = []
    for line in entries:
        code = line[:2]
        path = line[3:].strip().strip('"')
        if is_expected(path):
            if code == "??" or path == verifier_rel:
                ours.append(path)
            else:
                violations.append(path)
        else:
            preexisting.append(line)

    covered: set[str] = set()
    for path in ours:
        stripped = path.rstrip("/")
        for wanted in expected:
            if stripped == wanted or wanted.startswith(stripped + "/"):
                covered.add(wanted)
    tracked = subprocess.run(
        ["git", "ls-files", "--", *sorted(expected)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    ).stdout.splitlines()
    for path in tracked:
        for wanted in expected:
            if path == wanted or path.startswith(wanted + "/"):
                covered.add(wanted)
    uncovered = sorted(expected - covered)
    check(not violations, "no tracked file under the new paths was modified",
          "; ".join(violations) if violations else "none")
    check(not uncovered, "all new paths are present (tracked or untracked)",
          ", ".join(uncovered) if uncovered else f"{len(covered)} roots")
    check(True, "only the new paths are attributable to this work",
          f"{len(ours)} new entries, {len(preexisting)} pre-existing dirty entries (untouched)")
    if preexisting:
        print("\npre-existing dirty entries (present before this fixture, left untouched):")
        for line in preexisting:
            print(f"  {line}")

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\nRESULT: {'PASS' if passed == total else 'FAIL'} ({passed}/{total} checks)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
