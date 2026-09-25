#!/usr/bin/env python3
"""Normalize heterogeneous semantic-search runs into a blinded candidate pool.

This utility is deliberately a pool builder, not a relevance scorer. It maps
CocoIndex chunks, zvec source ranges, and Sense symbols to source line spans,
merges overlapping evidence per query, and keeps backend mappings in a
separate normalized-runs file. Draft target grades are never copied into the
pool or qrels.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any


class NormalizationError(ValueError):
    """Raised when a raw run cannot be safely mapped to source."""


@dataclass(frozen=True)
class Span:
    path: str
    start: int
    end: int
    origin: str
    detail: str = ""


def _source_path(root: pathlib.Path, path: str) -> pathlib.Path:
    normalized = path.removeprefix("kubernaut/").lstrip("/")
    candidate = root / normalized
    if not candidate.is_file():
        raise NormalizationError(f"source file not found for {path!r}")
    return candidate


def _source_lines(root: pathlib.Path, path: str) -> list[str]:
    return _source_path(root, path).read_text().splitlines()


def _span_text(root: pathlib.Path, span: Span) -> str:
    lines = _source_lines(root, span.path)
    return "\n".join(lines[span.start - 1 : span.end])


def _balanced_end(lines: list[str], start: int) -> int:
    """Return the closing-brace line for a declaration, tolerating strings."""
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


def _declaration_span(
    root: pathlib.Path, path: str, symbol: str, line_hint: int, kind: str
) -> Span:
    lines = _source_lines(root, path)
    name = symbol.rsplit(".", 1)[-1]
    escaped = re.escape(name)
    patterns = (
        re.compile(rf"^\s*func\s+(?:\([^)]*\)\s*)?{escaped}\b"),
        re.compile(rf"^\s*type\s+{escaped}\b"),
        re.compile(rf"^\s*(?:const|var)\s+(?:\([^)]*\)\s*)?{escaped}\b"),
    )
    candidates = [
        index + 1
        for index, line in enumerate(lines)
        if any(pattern.search(line) for pattern in patterns)
    ]
    if candidates:
        start = min(candidates, key=lambda value: abs(value - line_hint))
    else:
        start = max(1, min(line_hint, len(lines)))
    end = _balanced_end(lines, start) if kind in {"function", "method", "class", "type"} else start
    return Span(path, start, end, "sense", symbol)


def _coco_span(root: pathlib.Path, result: dict[str, Any]) -> Span:
    path = result["filepath"].removeprefix("kubernaut/")
    source = _source_path(root, path).read_text()
    code = result.get("code", "")
    position = source.find(code) if code else -1
    if position < 0:
        first_line = next((line.strip() for line in code.splitlines() if line.strip()), "")
        if not first_line:
            raise NormalizationError(f"empty CocoIndex snippet for {path!r}")
        line_number = next(
            (index for index, line in enumerate(source.splitlines(), 1) if first_line in line),
            None,
        )
        if line_number is None:
            raise NormalizationError(f"CocoIndex snippet not found in {path!r}")
        start = end = line_number
    else:
        start = source[:position].count("\n") + 1
        end = start + code.count("\n")
    return Span(path, start, end, "cocoindex", str(result.get("chunk_index", "")))


_ZVEC_HEADER = re.compile(
    r"^#(?P<rank>\d+)(?: \[[^\]]+\])? matchedBy=\S+ (?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$"
)


def _zvec_spans(root: pathlib.Path, response: dict[str, Any]) -> list[tuple[int, Span]]:
    content = response.get("result", {}).get("content", [])
    if not content or response.get("result", {}).get("isError"):
        raise NormalizationError("zvec response is missing or reports an error")
    text = content[0].get("text", "")
    blocks = re.split(r"(?=^#\d+(?: \[[^\]]+\])? matchedBy=)", text, flags=re.MULTILINE)
    output: list[tuple[int, Span]] = []
    for block in blocks:
        first_line = block.splitlines()[0] if block.splitlines() else ""
        match = _ZVEC_HEADER.match(first_line)
        if not match:
            continue
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        symbol_match = re.search(r"^symbol: (.+)$", block, flags=re.MULTILINE)
        detail = symbol_match.group(1) if symbol_match else ""
        output.append((int(match.group("rank")), Span(
            match.group("path"), start, end, "zvec-git", detail
        )))
    if not output:
        raise NormalizationError("zvec response contained no ranked source spans")
    return output


def _sense_spans(root: pathlib.Path, payload: dict[str, Any]) -> list[tuple[int, Span]]:
    output = []
    for rank, result in enumerate(payload.get("results", []), 1):
        output.append((rank, _declaration_span(
            root,
            result["file"],
            result.get("symbol", ""),
            int(result["line"]),
            result.get("kind", ""),
        )))
    if not output:
        raise NormalizationError("Sense response contained no ranked results")
    return output


def _draft_spans(root: pathlib.Path, draft: dict[str, Any]) -> dict[str, list[Span]]:
    output: dict[str, list[Span]] = {}
    for query in draft["queries"]:
        spans = []
        for target in query.get("proposed_targets", []):
            path, symbol = target["unit_id"].split("::", 1)
            lines = _source_lines(root, path)
            hint = next(
                (index for index, line in enumerate(lines, 1) if symbol.rsplit(".", 1)[-1] in line),
                1,
            )
            spans.append(_declaration_span(root, path, symbol, hint, "function"))
        output[query["id"]] = spans
    return output


def _components(spans: list[Span]) -> list[list[Span]]:
    groups: list[list[Span]] = []
    for span in sorted(spans, key=lambda item: (item.path, item.start, item.end)):
        current_end = max(item.end for item in groups[-1]) if groups else -1
        if groups and groups[-1][-1].path == span.path and span.start <= current_end:
            groups[-1].append(span)
        else:
            groups.append([span])
    return groups


def _unit_id(group: list[Span]) -> str:
    path = group[0].path
    return f"{path}::L{min(item.start for item in group)}-L{max(item.end for item in group)}"


def normalize(
    root: pathlib.Path,
    coco_path: pathlib.Path,
    zvec_path: pathlib.Path,
    sense_path: pathlib.Path,
    draft_path: pathlib.Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    coco = json.loads(coco_path.read_text())
    zvec = json.loads(zvec_path.read_text())
    sense = json.loads(sense_path.read_text())
    draft = json.loads(draft_path.read_text())
    raw_by_backend = {"cocoindex": coco, "zvec-git": zvec, "sense": sense}
    query_ids = [query["id"] for query in coco["queries"]]
    raw_rows = {
        backend: {query["id"]: query for query in payload["queries"]}
        for backend, payload in raw_by_backend.items()
    }
    draft_by_query = _draft_spans(root, draft)
    blinded_queries = []
    mapping: dict[str, Any] = {"queries": {}}

    for query_id in query_ids:
        spans_by_backend: dict[str, list[tuple[int, Span]]] = {}
        all_spans = list(draft_by_query.get(query_id, []))
        for backend, rows in raw_rows.items():
            row = rows[query_id]
            if backend == "cocoindex":
                ranked = [(rank, _coco_span(root, result)) for rank, result in enumerate(row["results"], 1)]
            elif backend == "zvec-git":
                ranked = _zvec_spans(root, row["response"])
            else:
                ranked = _sense_spans(root, row["results"])
            spans_by_backend[backend] = ranked
            all_spans.extend(span for _, span in ranked)

        groups = _components(all_spans)
        span_to_unit = {
            id(span): _unit_id(group)
            for group in groups
            for span in group
        }
        pool = []
        for group in groups:
            unit_id = _unit_id(group)
            start = min(item.start for item in group)
            end = max(item.end for item in group)
            path = group[0].path
            pool.append({
                "unit_id": unit_id,
                "path": path,
                "start_line": start,
                "end_line": end,
                "source": _span_text(root, Span(path, start, end, "pool")),
            })
        blinded_queries.append({"id": query_id, "candidates": pool})

        query_mapping = {}
        for backend, ranked in spans_by_backend.items():
            results = []
            seen = set()
            for rank, span in ranked:
                unit_id = span_to_unit[id(span)]
                if unit_id in seen:
                    continue
                seen.add(unit_id)
                results.append({
                    "unit_id": unit_id,
                    "rank": rank,
                    "path": span.path,
                    "start_line": span.start,
                    "end_line": span.end,
                    "detail": span.detail,
                })
            query_mapping[backend] = results
        mapping["queries"][query_id] = query_mapping

    run_rows = []
    for backend in raw_by_backend:
        run_rows.append({
            "backend": backend,
            "queries": [
                {"id": query_id, "results": mapping["queries"][query_id][backend]}
                for query_id in query_ids
            ],
        })
    normalized = {
        "schema_version": 1,
        "source": coco["source"],
        "result_limit": 10,
        "runs": run_rows,
    }
    blinded = {
        "schema_version": 1,
        "judgment_status": "pending-adjudication",
        "candidate_pool_complete": False,
        "source": coco["source"],
        "queries": blinded_queries,
    }
    return normalized, {"schema_version": 1, **mapping}, blinded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=pathlib.Path, required=True)
    parser.add_argument("--cocoindex", type=pathlib.Path, required=True)
    parser.add_argument("--zvec", type=pathlib.Path, required=True)
    parser.add_argument("--sense", type=pathlib.Path, required=True)
    parser.add_argument("--draft", type=pathlib.Path, required=True)
    parser.add_argument("--normalized-runs", type=pathlib.Path, required=True)
    parser.add_argument("--mapping", type=pathlib.Path, required=True)
    parser.add_argument("--blinded-pool", type=pathlib.Path, required=True)
    args = parser.parse_args()
    normalized, mapping, blinded = normalize(
        args.source_root,
        args.cocoindex,
        args.zvec,
        args.sense,
        args.draft,
    )
    args.normalized_runs.write_text(json.dumps(normalized, indent=2) + "\n")
    args.mapping.write_text(json.dumps(mapping, indent=2) + "\n")
    args.blinded_pool.write_text(json.dumps(blinded, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
