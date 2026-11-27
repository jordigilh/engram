#!/usr/bin/env python3
"""Regenerate `goldens.json` from the real Python PDF path.

This is developer tooling, not a test. It uses the deployed flow interpreter
because `process_pdf_file` requires pdfplumber and `_retain_document` uses the
real synthesis (sklearn/networkx):

    PYTHONPATH=src ~/.engram/venv/bin/python \
        rust/crates/engram-ingest/fixtures/pdf/generate_goldens.py

The emitted record captures pdfplumber's per-page text, the exact rendered
content the Python `process_pdf_file` produces, and every retain call that
`_retain_document` would send for the fixture. Rust tests compare against it
byte-for-byte; review any diff together with the fixture or Python changes.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import platform
import subprocess

import pdfplumber

import engram.flows.configured as configured

FIXTURES = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path(__file__).resolve().parents[5]
SOURCE_TAG = "manual"
RELATIVE = "guide.pdf"
TIMESTAMP = "2026-11-27T09:30:00.123456"


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def rendered_from_pages(pages: list[str]) -> str:
    return "\n\n".join(
        f"--- Page {index + 1} ---\n\n{text}"
        for index, text in enumerate(pages)
        if text.strip()
    )


def main() -> None:
    fixture = FIXTURES / "guide.pdf"
    with pdfplumber.open(str(fixture)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    rendered = rendered_from_pages(pages)

    captured: list[dict] = []

    def capture(**kwargs):
        captured.append(kwargs)

    configured.hindsight_retain = capture
    configured._retain_document(
        bank_id="fixture-bank",
        content=rendered,
        source_tag=SOURCE_TAG,
        relative=RELATIVE,
        timestamp=TIMESTAMP,
    )

    payload = {
        "provenance": {
            "python": platform.python_version(),
            "pdfplumber": getattr(pdfplumber, "__version__", "unknown"),
            "source_commit": git_commit(),
            "sources": {
                "src/engram/chunking.py": sha256_file(ROOT / "src/engram/chunking.py"),
                "src/engram/flows/configured.py": sha256_file(
                    ROOT / "src/engram/flows/configured.py"
                ),
                "src/engram/synthesis.py": sha256_file(
                    ROOT / "src/engram/synthesis.py"
                ),
            },
            "fixture": {
                "file": fixture.name,
                "sha256": sha256_file(fixture),
                "size_bytes": fixture.stat().st_size,
            },
        },
        "source_tag": SOURCE_TAG,
        "relative": RELATIVE,
        "timestamp": TIMESTAMP,
        "pages": pages,
        "rendered": rendered,
        "expected": [
            {
                "document_id": call["document_id"],
                "content": call["content"],
                "metadata": call["metadata"],
                "tags": call["tags"],
                "timestamp": call["timestamp"],
            }
            for call in captured
        ],
    }
    (FIXTURES / "goldens.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
