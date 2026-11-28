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

import asyncio
import hashlib
import json
import os
import pathlib
import platform
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

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


def timestamp_cases() -> list[dict]:
    cases = []
    for seconds, micros, offset in [
        (1_700_000_000, 0, 0),
        (1_700_000_000, 123_456, 0),
        (1_700_000_000, 0, 3600),
        (0, 999_999, -18_000),
    ]:
        moment = datetime.fromtimestamp(
            seconds, timezone(timedelta(seconds=offset))
        ).replace(tzinfo=None, microsecond=micros)
        cases.append(
            {
                "seconds": seconds,
                "micros": micros,
                "offset_seconds": offset,
                "expected": moment.isoformat(),
            }
        )
    return cases


def mtime_cases() -> list[dict]:
    """Python's actual stat/float/isoformat result for fixed nanosecond mtimes.

    The Rust test sets the same mtime and converts with a zero offset (UTC), so
    this pins the stat and rounding path, not just formatter input.
    """
    cases = []
    for nanos in (0, 1_700_000_000_123_456_789, 1_700_000_000_500_000_000,
                  1_700_000_000_999_999_999):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            path = pathlib.Path(handle.name)
        try:
            os.utime(path, ns=(nanos, nanos))
            stat = path.stat()
            moment = datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).replace(tzinfo=None)
            cases.append(
                {
                    "seconds": nanos // 1_000_000_000,
                    "nanos": nanos % 1_000_000_000,
                    "expected": moment.isoformat(),
                }
            )
        finally:
            path.unlink(missing_ok=True)
    return cases


def main() -> None:
    fixture = FIXTURES / "guide.pdf"
    with pdfplumber.open(str(fixture)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    diagnostic_render = rendered_from_pages(pages)
    with pdfplumber.open(str(FIXTURES / "encrypted_empty.pdf")) as pdf:
        encrypted_pages = [page.extract_text() or "" for page in pdf.pages]
    encrypted_empty_rendered = rendered_from_pages(encrypted_pages)

    captured_calls: list[dict] = []
    captured_document: dict = {}

    original_retain_document = configured._retain_document

    def capture_call(**kwargs):
        captured_calls.append(kwargs)

    def capture_document(**kwargs):
        captured_document.update(kwargs)
        original_retain_document(**kwargs)

    class FakeFile:
        file_path = fixture

    configured.hindsight_retain = capture_call
    configured._retain_document = capture_document
    configured._file_timestamp = lambda _path: TIMESTAMP
    asyncio.run(
        configured.process_pdf_file(FakeFile(), FIXTURES, "fixture-bank", SOURCE_TAG)
    )

    rendered = captured_document["content"]
    relative = captured_document["relative"]
    timestamp = captured_document["timestamp"]
    assert rendered == diagnostic_render, "pdfplumber reconstruction drifted"
    assert relative == RELATIVE, relative
    assert timestamp == TIMESTAMP, timestamp

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
        "relative": relative,
        "timestamp": timestamp,
        "pages": pages,
        "rendered": rendered,
        "encrypted_empty_rendered": encrypted_empty_rendered,
        "timestamp_cases": timestamp_cases(),
        "mtime_cases": mtime_cases(),
        "expected": [
            {
                "document_id": call["document_id"],
                "content": call["content"],
                "metadata": call["metadata"],
                "tags": call["tags"],
                "timestamp": call["timestamp"],
            }
            for call in captured_calls
        ],
    }
    (FIXTURES / "goldens.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
