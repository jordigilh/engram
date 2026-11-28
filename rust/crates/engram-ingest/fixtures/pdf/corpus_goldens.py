#!/usr/bin/env python3
"""Generate the pdfplumber oracle for the PDF parity corpus.

    PYTHONPATH=src ~/.engram/venv/bin/python \
        rust/crates/engram-ingest/fixtures/pdf/corpus_goldens.py
"""
from __future__ import annotations

import json
import pathlib
import platform
import subprocess

import pdfplumber

HERE = pathlib.Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
ROOT = pathlib.Path(__file__).resolve().parents[5]


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def render(pages: list[str]) -> str:
    return "\n\n".join(
        f"--- Page {index + 1} ---\n\n{text}"
        for index, text in enumerate(pages)
        if text.strip()
    )


def main() -> None:
    cases = json.loads((CORPUS / "cases.json").read_text(encoding="utf-8"))
    rendered_cases = []
    for case in cases:
        path = CORPUS / case["file"]
        with pdfplumber.open(str(path)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        rendered_cases.append(
            {
                "name": case["name"],
                "file": case["file"],
                "pages": pages,
                "rendered": render(pages),
            }
        )
    payload = {
        "provenance": {
            "python": platform.python_version(),
            "pdfplumber": getattr(pdfplumber, "__version__", "unknown"),
            "source_commit": git_commit(),
        },
        "cases": rendered_cases,
    }
    (CORPUS / "goldens.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(rendered_cases)} oracle cases")


if __name__ == "__main__":
    main()
