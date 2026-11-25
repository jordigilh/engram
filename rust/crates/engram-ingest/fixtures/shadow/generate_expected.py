#!/usr/bin/env python3
"""Generate the frozen shadow fixture from the real Python docs flow.

Run from the repository root with the deployed flow interpreter:

    PYTHONPATH=src ~/.engram/venv/bin/python \
        rust/crates/engram-ingest/fixtures/shadow/generate_expected.py

It uses CocoIndex's own `walk_dir`/`PatternFilePathMatcher` for matching and the
real `_retain_document` (with `hindsight_retain` captured) for operations.
Timestamps are disabled because git does not preserve mtimes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import platform
import subprocess

from cocoindex.connectors import localfs
from cocoindex.resources.file import PatternFilePathMatcher

import engram.flows.configured as configured

ROOT = pathlib.Path(__file__).resolve().parents[5]
TREE = pathlib.Path(__file__).resolve().parent / "tree"
SOURCE_TAG = "repo"


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def collect_operations() -> list[dict]:
    walker = localfs.walk_dir(
        TREE,
        recursive=True,
        path_matcher=PatternFilePathMatcher(["**/*.md"], []),
    )
    files = []
    async for file in walker:
        files.append(file)

    operations: list[dict] = []
    captured: list[dict] = []

    def capture(**kwargs):
        captured.append(kwargs)

    configured.hindsight_retain = capture
    for file in sorted(
        files,
        key=lambda item: configured._relative_path(item, TREE),
    ):
        content = await file.read_text()
        if not content.strip():
            continue
        relative = configured._relative_path(file, TREE)
        captured.clear()
        configured._retain_document(
            bank_id="fixture-bank",
            content=content,
            source_tag=SOURCE_TAG,
            relative=relative,
            timestamp=None,
        )
        for ordinal, call in enumerate(captured):
            operations.append(
                {
                    "path": relative,
                    "ordinal": ordinal,
                    "document_id": call["document_id"],
                    "content": call["content"],
                    "content_sha256": hashlib.sha256(
                        call["content"].encode("utf-8")
                    ).hexdigest(),
                    "metadata": dict(sorted(call["metadata"].items())),
                    "tags": call["tags"],
                    "timestamp": call["timestamp"],
                }
            )
    operations.sort(key=lambda op: (op["path"], op["ordinal"]))
    return operations


def main() -> None:
    operations = asyncio.run(collect_operations())
    output = "".join(
        json.dumps(op, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for op in operations
    )
    (TREE.parent / "expected.jsonl").write_text(output, encoding="utf-8")
    provenance = {
        "python": platform.python_version(),
        "cocoindex": __import__("cocoindex").__version__,
        "source_commit": git_commit(),
        "tree": "tree",
        "source_tag": SOURCE_TAG,
        "timestamps": False,
        "operations": len(operations),
    }
    (TREE.parent / "expected.meta.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
