#!/usr/bin/env python3
"""Generate the PDF parity corpus and its pdfplumber oracle.

Run from anywhere:

    python3 rust/crates/engram-ingest/fixtures/pdf/corpus_make.py
    PYTHONPATH=src ~/.engram/venv/bin/python \
        rust/crates/engram-ingest/fixtures/pdf/corpus_goldens.py
"""
from __future__ import annotations

import json
import pathlib

from make_fixture import build_streams, escape

HERE = pathlib.Path(__file__).resolve().parent
CORPUS = HERE / "corpus"


def text_at(x: int, y: int, text: str, font: str = "F1", size: int = 12, rotated: bool = False) -> str:
    matrix = f"0 1 -1 0 {x} {y}" if rotated else f"1 0 0 1 {x} {y}"
    return f"BT /{font} {size} Tf {matrix} Tm ({escape(text)}) Tj ET"


def block_at(x: int, y: int, lines: list[str]) -> str:
    operations = [f"BT /F1 12 Tf 1 0 0 1 {x} {y} Tm 14 TL"]
    for index, line in enumerate(lines):
        if index:
            operations.append("T*")
        operations.append(f"({escape(line)}) Tj")
    operations.append("ET")
    return "\n".join(operations)


def page(*operations: str) -> bytes:
    return ("\n".join(operations) + "\n").encode("latin-1")


CASES = {
    "two_column": [
        page(
            *[
                operation
                for row, y in enumerate((720, 700, 680), start=1)
                for operation in (
                    text_at(72, y, f"Left column row {row}"),
                    text_at(330, y, f"Right column row {row}"),
                )
            ]
        )
    ],
    "table": [
        page(
            text_at(72, 720, "Region"),
            text_at(220, 720, "Revenue"),
            text_at(360, 720, "Growth"),
            text_at(72, 700, "North"),
            text_at(220, 700, "12,400"),
            text_at(360, 700, "12%"),
            text_at(72, 680, "South"),
            text_at(220, 680, "9,800"),
            text_at(360, 680, "8%"),
            text_at(72, 660, "West"),
            text_at(220, 660, "15,100"),
            text_at(360, 660, "21%"),
        )
    ],
    "two_fonts": [
        page(
            text_at(72, 720, "Helvetica heading", "F1", 16),
            text_at(72, 698, "Times body line one.", "F2"),
            text_at(72, 680, "Times body line two.", "F2"),
            text_at(72, 662, "Helvetica footer.", "F1"),
        )
    ],
    "long_page": [page(*[text_at(72, 740 - 14 * i, f"Line {i + 1} of the long report.") for i in range(48)])],
    "paragraph_gaps": [
        page(
            block_at(72, 720, ["First paragraph starts here.", "It has two lines."]),
            block_at(72, 660, ["Second paragraph starts here.", "It also has two lines."]),
        )
    ],
    "unicode_winansi": [
        page(
            text_at(72, 720, "Café résumé – naïve façade"),
            text_at(72, 700, "“Quoted text” and € 12.50"),
        )
    ],
    "delimiters": [
        page(
            text_at(72, 720, "R&D (draft) \\ review"),
            text_at(72, 700, "A & B (C) [D] {E}"),
        )
    ],
    "ascii85_chain": [
        page(
            text_at(72, 720, "ASCII85 encoded page content."),
            text_at(72, 700, "Decoded through the shared bound."),
        )
    ],
    "many_pages": [
        page(text_at(72, 720, f"Report page {index} of the long manual."))
        if index % 7
        else page()
        for index in range(1, 61)
    ],
    "multi_page_blank_middle": [
        page(text_at(72, 720, "First page body.")),
        page(),
        page(text_at(72, 720, "Third page body.")),
    ],
}


def main() -> None:
    CORPUS.mkdir(exist_ok=True)
    manifest = []
    for name, streams in CASES.items():
        path = CORPUS / f"{name}.pdf"
        build_streams(streams, path, ascii85=name == "ascii85_chain")
        manifest.append({"name": name, "file": path.name})
    (CORPUS / "cases.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(manifest)} corpus PDFs to {CORPUS}")


if __name__ == "__main__":
    main()
