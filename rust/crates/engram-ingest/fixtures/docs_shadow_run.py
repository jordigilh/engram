#!/usr/bin/env python3
"""One-command docs shadow gate for issue #214 slice 3.

Freezes the real `docs/` tree at a git revision, captures the real CocoIndex
docs flow over a loopback Hindsight server, derives a second tree by removing
one real file and one real heading section, captures the flow again, then runs
the Rust comparator against both snapshots and writes the measured report.

Run with the deployed flow interpreter (it must import cocoindex and engram):

    ~/.engram/venv/bin/python \\
        rust/crates/engram-ingest/fixtures/docs_shadow_run.py --rev origin/main

The default output is `docs/findings/214-docs-shadow-gate.{json,md}`. Nothing
is merged and no service is touched.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict


REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
HERE = pathlib.Path(__file__).resolve().parent
ORACLE = HERE / "docs_shadow_oracle.py"
DEFAULT_OUT = REPO_ROOT / "docs/findings"


def run(command: list[str], *, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, text=True)


def freeze(repo: pathlib.Path, rev: str, destination: pathlib.Path) -> None:
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=tar", rev, "docs"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    if not (destination / "docs").is_dir():
        raise SystemExit(f"git archive {rev} did not produce a docs/ tree")


def resolve_revision(repo: pathlib.Path, rev: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{rev}^{{commit}}"], text=True
    ).strip()


def set_subsecond_mtime(root: pathlib.Path) -> None:
    """Exercise Python's microsecond timestamp formatting, not just seconds."""
    paths = sorted(root.rglob("*.md"))
    if not paths:
        return
    stat = paths[0].stat()
    seconds = stat.st_mtime_ns - (stat.st_mtime_ns % 1_000_000_000)
    os.utime(paths[0], ns=(stat.st_atime_ns, seconds + 123_456_789))


def retained_ids_by_path(dump: dict) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for retain in dump["retains"]:
        result[retain["relative_path"]].add(retain["document_id"])
    return result


def capture_oracle(
    root: pathlib.Path,
    out: pathlib.Path,
    work: pathlib.Path,
    repo: pathlib.Path,
    extra: list[str],
) -> dict:
    command = [
        sys.executable,
        str(ORACLE),
        "--root",
        str(root),
        "--out",
        str(out),
        "--repo-root",
        str(repo),
        "--work-dir",
        str(work),
        *extra,
    ]
    completed = run(command)
    if completed.returncode:
        raise SystemExit(f"oracle capture failed for {root}")
    return json.loads(out.read_text(encoding="utf-8"))


def choose_mutations(after_root: pathlib.Path, before: dict) -> tuple[pathlib.Path, pathlib.Path]:
    by_path: dict[str, list[dict]] = defaultdict(list)
    for retain in before["retains"]:
        if retain["relative_path"]:
            by_path[retain["relative_path"]].append(retain)
    for retains in by_path.values():
        retains.sort(key=lambda retain: retain["sequence"])

    edit = None
    for relative in sorted(by_path):
        retains = by_path[relative]
        if len(retains) < 2 or not retains[-1]["chunk_key"]:
            continue
        path = after_root / relative
        text = path.read_text(encoding="utf-8")
        headings = list(re.finditer(r"(?m)^#{1,6}\s", text))
        if not headings or headings[-1].start() == 0:
            continue
        edit = path
        break
    if edit is None:
        raise SystemExit("no frozen docs file has a removable heading section")

    removed = None
    for relative in sorted(by_path, reverse=True):
        path = after_root / relative
        if path != edit:
            removed = path
            break
    if removed is None:
        raise SystemExit("no frozen docs file is available for the remove-file scenario")
    return edit, removed


def apply_mutations(after_root: pathlib.Path, before: dict) -> tuple[str, str]:
    edit, removed = choose_mutations(after_root, before)
    stat = edit.stat()
    text = edit.read_text(encoding="utf-8")
    headings = list(re.finditer(r"(?m)^#{1,6}\s", text))
    edited_text = text[: headings[-1].start()].rstrip() + "\n"
    edit.write_text(edited_text, encoding="utf-8")
    os.utime(edit, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    removed.unlink()
    return (
        edit.relative_to(after_root).as_posix(),
        removed.relative_to(after_root).as_posix(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rev", default="origin/main")
    parser.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT)
    parser.add_argument("--tag", default="engram-docs")
    parser.add_argument("--bank", default="engram-docs")
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get("ENGRAM_SHADOW_PG_DSN"),
    )
    parser.add_argument("--cargo", default="cargo")
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    includes = args.include or ["**/*.md"]
    oracle_extra = ["--tag", args.tag, "--bank", args.bank]
    for pattern in includes:
        oracle_extra.extend(["--include", pattern])
    for pattern in args.exclude:
        oracle_extra.extend(["--exclude", pattern])
    if args.pg_dsn:
        oracle_extra.extend(["--pg-dsn", args.pg_dsn])

    work = pathlib.Path(tempfile.mkdtemp(prefix="engram-docs-shadow-"))
    try:
        before_root = work / "before"
        after_root = work / "after"
        before_root.mkdir()
        revision = resolve_revision(repo, args.rev)
        freeze(repo, args.rev, before_root)
        shutil.copytree(before_root, after_root, symlinks=True, copy_function=shutil.copy2)
        set_subsecond_mtime(before_root / "docs")
        set_subsecond_mtime(after_root / "docs")

        before_json = work / "oracle-before.json"
        after_json = work / "oracle-after.json"
        before = capture_oracle(before_root / "docs", before_json, work / "oracle-before-work", repo, oracle_extra)
        if before["provenance"]["flow_source_commit"] != revision:
            print(
                f"warning: frozen docs revision {revision} differs from flow source "
                f"{before['provenance']['flow_source_commit']}",
                file=sys.stderr,
            )
        edited, removed = apply_mutations(after_root / "docs", before)
        after = capture_oracle(after_root / "docs", after_json, work / "oracle-after-work", repo, oracle_extra)

        before_ids = {retain["document_id"] for retain in before["retains"]}
        after_ids = {retain["document_id"] for retain in after["retains"]}
        vanished = before_ids - after_ids
        ids_before = retained_ids_by_path(before)
        ids_after = retained_ids_by_path(after)
        edited_vanished = ids_before.get(edited, set()) - ids_after.get(edited, set())
        removed_vanished = ids_before.get(removed, set())
        print(f"mutation: edited {edited}, removed {removed}; {len(vanished)} oracle document ids vanished")
        if not edited_vanished:
            raise SystemExit(f"editing {edited} removed no oracle document ids")
        if not removed_vanished:
            raise SystemExit(f"removing {removed} removed no oracle document ids")
        if not vanished:
            raise SystemExit("the mutation removed no oracle document ids")

        report_dir = work / "report"
        report_dir.mkdir()
        command = [
            args.cargo,
            "run",
            "--quiet",
            "--manifest-path",
            str(repo / "rust/Cargo.toml"),
            "-p",
            "engram-ingest",
            "--example",
            "docs_shadow",
            "--",
            "--root-before",
            str(before_root / "docs"),
            "--root-after",
            str(after_root / "docs"),
            "--oracle-before",
            str(before_json),
            "--oracle-after",
            str(after_json),
            "--report-json",
            str(report_dir / "report.json"),
            "--report-markdown",
            str(report_dir / "report.md"),
            "--revision",
            revision,
            "--mutation",
            f"edited {edited}; removed {removed}",
            "--tag",
            args.tag,
            "--bank",
            args.bank,
        ]
        for pattern in includes:
            command.extend(["--include", pattern])
        for pattern in args.exclude:
            command.extend(["--exclude", pattern])
        completed = run(command, cwd=repo)

        args.out_dir.mkdir(parents=True, exist_ok=True)
        report_json = args.out_dir / "214-docs-shadow-gate.json"
        report_markdown = args.out_dir / "214-docs-shadow-gate.md"
        if not (report_dir / "report.json").is_file():
            raise SystemExit(f"docs_shadow example failed with exit code {completed.returncode}")
        shutil.copyfile(report_dir / "report.json", report_json)
        shutil.copyfile(report_dir / "report.md", report_markdown)
        print(f"report: {report_markdown}")
        print(f"json:   {report_json}")
        if args.keep:
            print(f"work:   {work}")
        if completed.returncode:
            raise SystemExit(completed.returncode)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
