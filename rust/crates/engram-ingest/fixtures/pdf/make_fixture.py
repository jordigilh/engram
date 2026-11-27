#!/usr/bin/env python3
"""Generate the redistributable PDF parity fixture (`guide.pdf`).

This is developer tooling, not a test. The PDF is hand-assembled so the
repository carries no third-party document and no PDF-writer dependency:

- standard Helvetica with WinAnsi encoding (no embedded font);
- pages 1 and 3 contain text, including Latin-1 accents, an en dash,
  PDF-escaped delimiters, an indented line, and a paragraph gap;
- page 2 is completely blank;
- page 4 contains only spaces.

Run from anywhere:

    python3 rust/crates/engram-ingest/fixtures/pdf/make_fixture.py
"""
import pathlib
import zlib

FIXTURE = pathlib.Path(__file__).with_name("guide.pdf")
BLANK_FIXTURE = pathlib.Path(__file__).with_name("blank.pdf")


def escape(text: str) -> str:
    escaped = []
    for byte in text.encode("cp1252"):
        char = chr(byte)
        escaped.append("\\" + char if char in "()\\" else char)
    return "".join(escaped)


def content(lines: list[str]) -> bytes:
    if not lines:
        return b""
    operators = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for index, line in enumerate(lines):
        if index:
            operators.append("T*")
        operators.append(f"({escape(line)}) Tj")
    operators.append("ET")
    return ("\n".join(operators) + "\n").encode("latin-1")


def build(pages: list[list[str]], path: pathlib.Path) -> None:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )
    pages_id = len(objects) + 1
    add(b"")  # pages root, filled in once the page objects exist
    page_ids = []
    for lines in pages:
        stream = content(lines)
        body = zlib.compress(stream) if stream else b""
        contents = add(
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(body)
            + body
            + b"\nendstream"
        )
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R "
                    f"/MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> "
                    f"/Contents {contents} 0 R >>"
                ).encode()
            )
        )
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    )
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(out)


PAGES = [
    [
        "Quarterly Report",
        "Revenue grew 12% in Café operations across every region this quarter.",
        "R&D (draft) review: naïve façade.",
    ],
    [],
    [
        "Summary",
        "Café résumé – naïve façade",
        "",
        "  Indented detail stays indented.",
    ],
    ["   "],
]


if __name__ == "__main__":
    build(PAGES, FIXTURE)
    # Text-less document: no page has a non-whitespace text object. It pins the
    # Python `process_pdf_file` early return on the scan side.
    build([[], ["   "]], BLANK_FIXTURE)
