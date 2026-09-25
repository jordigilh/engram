#!/usr/bin/env python3
"""Replay the synthetic fixture through CocoIndex, zvec-git, and Sense.

The fixture truth is qualified source symbols, while the backends return
chunks, line ranges, or package-qualified symbols. This command captures the
raw ranked responses and maps every result to the manifest's stable unit IDs
before writing evaluator-shaped runs.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


class ReplayError(ValueError):
    """Raised when a backend response cannot be mapped safely."""


@dataclass(frozen=True)
class UnitSpan:
    unit_id: str
    path: str
    symbol: str
    start: int
    end: int


_ZVEC_HEADER = re.compile(
    r"^#(?P<rank>\d+)(?: \[[^\]]+\])? matchedBy=\S+ "
    r"(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$"
)


def _balanced_end(lines: list[str], start: int) -> int:
    text = "\n".join(lines[start - 1 :])
    opening = text.find("{")
    if opening < 0:
        return start

    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = opening
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 1
        elif quote:
            if quote == "`":
                if char == "`":
                    quote = ""
            elif escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char == "/" and next_char == "/":
            line_comment = True
            index += 1
        elif char == "/" and next_char == "*":
            block_comment = True
            index += 1
        elif char in ('"', "'", "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start + text[: index + 1].count("\n")
        index += 1
    return min(len(lines), start + text.count("\n"))


def _unit_spans(fixture: pathlib.Path, manifest: dict[str, Any]) -> list[UnitSpan]:
    spans = []
    for unit in manifest["units"]:
        path = unit["path"]
        symbol = unit["symbol"]
        lines = (fixture / path).read_text().splitlines()
        if "start_line" in unit or "end_line" in unit:
            start = unit.get("start_line")
            end = unit.get("end_line")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or start < 1
                or end < start
                or end > len(lines)
            ):
                raise ReplayError(f"invalid manifest source span for {path}::{symbol}")
            spans.append(UnitSpan(unit["unit_id"], path, symbol, start, end))
            continue
        name = re.escape(symbol.rsplit(".", 1)[-1])
        patterns = (
            re.compile(rf"^\s*func\s+(?:\([^)]*\)\s*)?{name}\b"),
            re.compile(rf"^\s*type\s+{name}\b"),
            re.compile(rf"^\s*(?:const|var)\s+(?:\([^)]*\)\s*)?{name}\b"),
        )
        candidates = [
            index + 1
            for index, line in enumerate(lines)
            if any(pattern.search(line) for pattern in patterns)
        ]
        if not candidates:
            raise ReplayError(f"manifest symbol declaration not found: {path}::{symbol}")
        start = candidates[0]
        end = _balanced_end(lines, start)
        spans.append(UnitSpan(unit["unit_id"], path, symbol, start, end))
    return spans


def _relative_path(path: str, repo_tag: str | None = None) -> str:
    path = path.removeprefix("/")
    if repo_tag:
        path = path.removeprefix(repo_tag + "/")
    return path


def _units_for_span(
    units: list[UnitSpan], path: str, start: int, end: int
) -> list[UnitSpan]:
    matches = [
        unit
        for unit in units
        if unit.path == path and unit.start <= end and unit.end >= start
    ]
    return sorted(matches, key=lambda unit: (unit.start, unit.end, unit.unit_id))


def _code_span(fixture: pathlib.Path, path: str, code: str) -> tuple[int, int]:
    source = (fixture / path).read_text()
    position = source.find(code)
    if position >= 0:
        start = source[:position].count("\n") + 1
        return start, start + code.count("\n")

    first_line = next((line.strip() for line in code.splitlines() if line.strip()), "")
    if not first_line:
        raise ReplayError(f"empty backend snippet for {path!r}")
    for line_number, line in enumerate(source.splitlines(), 1):
        if first_line in line:
            return line_number, line_number
    raise ReplayError(f"backend snippet not found in {path!r}")


def _map_chunk(
    units: list[UnitSpan], path: str, start: int, end: int
) -> list[str]:
    matches = _units_for_span(units, path, start, end)
    if not matches:
        raise ReplayError(f"backend span does not intersect a manifest unit: {path}:{start}-{end}")
    return [unit.unit_id for unit in matches]


def _map_sense_result(units: list[UnitSpan], result: dict[str, Any]) -> list[str]:
    path = result["file"]
    symbol = result["symbol"]
    normalized_symbol = re.sub(r"[^\w]", "", symbol, flags=re.UNICODE).casefold()
    line = result.get("line")
    matches = [
        unit
        for unit in units
        if unit.path == path
        and (
            normalized_symbol
            in {
                re.sub(r"[^\w]", "", unit.symbol, flags=re.UNICODE).casefold(),
                re.sub(r"[^\w]", "", unit.symbol.rsplit(".", 1)[-1], flags=re.UNICODE).casefold(),
            }
            or normalized_symbol.endswith(
                re.sub(r"[^\w]", "", unit.symbol, flags=re.UNICODE).casefold()
            )
        )
        and (not isinstance(line, int) or unit.start <= line <= unit.end)
    ]
    if len(matches) != 1:
        raise ReplayError(f"Sense symbol did not map uniquely: {path}::{symbol}")
    return [matches[0].unit_id]


def _dedupe_results(
    ranked_ids: list[tuple[int, list[str]]],
) -> list[dict[str, Any]]:
    results = []
    seen = set()
    for rank, unit_ids in ranked_ids:
        for unit_id in unit_ids:
            if unit_id in seen:
                continue
            seen.add(unit_id)
            results.append({"unit_id": unit_id, "backend_rank": rank})
    return results


def _run_zvec(
    binary: pathlib.Path,
    root: pathlib.Path,
    queries: list[dict[str, Any]],
    model_cache: pathlib.Path | None,
) -> list[dict[str, Any]]:
    rows = []
    for query in queries:
        command = [
            str(binary),
            "--mode",
            "direct",
            "--refresh",
            "off",
            "--limit",
            "10",
            "--compact",
            "--preview",
            "full",
        ]
        if model_cache:
            command.extend(["--model-cache", str(model_cache), "--device", "cpu"])
        command.extend(["--hybrid", query["query"]])
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise ReplayError(
                f"zvec failed for {query['id']}: {completed.stderr.strip()}"
            )
        rows.append({
            "id": query["id"],
            "query": query["query"],
            "response": {"stdout": completed.stdout, "stderr": completed.stderr},
        })
    return rows


def _zvec_results(
    units: list[UnitSpan], response: dict[str, Any]
) -> list[dict[str, Any]]:
    ranked = []
    for line in response["stdout"].splitlines():
        match = _ZVEC_HEADER.match(line)
        if not match:
            continue
        path = _relative_path(match["path"])
        start = int(match["start"])
        end = int(match["end"] or start)
        ranked.append((int(match["rank"]), _map_chunk(units, path, start, end)))
    if not ranked:
        raise ReplayError("zvec response contained no ranked source spans")
    return _dedupe_results(ranked)


def _run_sense(
    binary: pathlib.Path,
    root: pathlib.Path,
    queries: list[dict[str, Any]],
    language: str = "go",
) -> list[dict[str, Any]]:
    rows = []
    for query in queries:
        completed = subprocess.run(
            [
                str(binary),
                "search",
                query["query"],
                "--limit",
                "10",
                "--language",
                language,
                "--json",
            ],
            cwd=root,
            env={**os.environ, "NO_COLOR": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise ReplayError(
                f"Sense failed for {query['id']}: {completed.stderr.strip()}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"Sense returned invalid JSON for {query['id']}") from exc
        rows.append({"id": query["id"], "query": query["query"], "response": payload})
    return rows


def _run_cocoindex(
    queries: list[dict[str, Any]], pg_url: str, table: str, repo_tag: str
) -> list[dict[str, Any]]:
    os.environ["COCOINDEX_PG_URL"] = pg_url
    from engram.search.kubernaut import search_code

    rows = []
    for query in queries:
        results = search_code(
            query["query"],
            limit=10,
            mode="hybrid",
            repo=repo_tag,
            branch="main",
            table=table,
        )
        rows.append({"id": query["id"], "query": query["query"], "results": results})
    return rows


def _coco_results(
    fixture: pathlib.Path,
    units: list[UnitSpan],
    row: dict[str, Any],
    repo_tag: str,
) -> list[dict[str, Any]]:
    ranked = []
    for rank, result in enumerate(row["results"], 1):
        path = _relative_path(result["filepath"], repo_tag)
        start, end = _code_span(fixture, path, result.get("code", ""))
        ranked.append((rank, _map_chunk(units, path, start, end)))
    return _dedupe_results(ranked)


def _sense_results(units: list[UnitSpan], row: dict[str, Any]) -> list[dict[str, Any]]:
    ranked = [
        (rank, _map_sense_result(units, result))
        for rank, result in enumerate(row["response"].get("results", []), 1)
    ]
    if not ranked:
        raise ReplayError("Sense response contained no ranked symbols")
    return _dedupe_results(ranked)


def replay(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture = args.fixture.resolve()
    manifest = json.loads((fixture / "manifest.json").read_text())
    truth = json.loads((fixture / "truth.json").read_text())
    queries = truth["queries"]
    units = _unit_spans(fixture, manifest)
    source = dict(json.loads((fixture / "qrels.json").read_text())["source"])
    source["fixture"] = manifest["fixture_id"]

    coco_rows = _run_cocoindex(queries, args.coco_pg_url, args.coco_table, args.coco_repo_tag)
    zvec_rows = _run_zvec(args.zvec_binary, args.zvec_root, queries, args.model_cache)
    sense_rows = _run_sense(
        args.sense_binary, args.sense_root, queries, manifest["language"]
    )
    by_backend = {
        "cocoindex": {row["id"]: row for row in coco_rows},
        "zvec-git": {row["id"]: row for row in zvec_rows},
        "sense": {row["id"]: row for row in sense_rows},
    }

    normalized_runs = []
    for backend in ("cocoindex", "zvec-git", "sense"):
        normalized_queries = []
        for query in queries:
            row = by_backend[backend][query["id"]]
            if backend == "cocoindex":
                results = _coco_results(fixture, units, row, args.coco_repo_tag)
            elif backend == "zvec-git":
                results = _zvec_results(units, row["response"])
            else:
                results = _sense_results(units, row)
            normalized_queries.append({"id": query["id"], "results": results})
        normalized_runs.append({"backend": backend, "queries": normalized_queries})

    raw = {
        "schema_version": 1,
        "suite_id": truth["suite_id"],
        "fixture_id": manifest["fixture_id"],
        "source": source,
        "result_limit": 10,
        "backends": {
            "cocoindex": {"table": args.coco_table, "repo_tag": args.coco_repo_tag, "queries": coco_rows},
            "zvec-git": {"root": str(args.zvec_root), "queries": zvec_rows},
            "sense": {"root": str(args.sense_root), "queries": sense_rows},
        },
    }
    normalized = {
        "schema_version": 1,
        "suite_id": truth["suite_id"],
        "fixture_id": manifest["fixture_id"],
        "source": source,
        "result_limit": 10,
        "runs": normalized_runs,
    }
    return raw, normalized


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=pathlib.Path, required=True)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--coco-pg-url", default="postgresql://hindsight:hindsight@localhost:5432/hindsight")
    parser.add_argument("--coco-table", required=True)
    parser.add_argument("--coco-repo-tag", required=True)
    parser.add_argument("--zvec-binary", type=pathlib.Path, required=True)
    parser.add_argument("--zvec-root", type=pathlib.Path, required=True)
    parser.add_argument("--model-cache", type=pathlib.Path)
    parser.add_argument("--sense-binary", type=pathlib.Path, required=True)
    parser.add_argument("--sense-root", type=pathlib.Path, required=True)
    args = parser.parse_args()

    try:
        raw, normalized = replay(args)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "raw-runs.json").write_text(json.dumps(raw, indent=2) + "\n")
        (args.output_dir / "normalized-runs.json").write_text(json.dumps(normalized, indent=2) + "\n")
    except (OSError, KeyError, ReplayError, subprocess.SubprocessError) as exc:
        print(f"replay error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
