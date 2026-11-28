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
import hashlib
import pathlib
import zlib

FIXTURE = pathlib.Path(__file__).with_name("guide.pdf")
BLANK_FIXTURE = pathlib.Path(__file__).with_name("blank.pdf")
BROKEN_FIXTURE = pathlib.Path(__file__).with_name("broken.pdf")
ENCRYPTED_EMPTY_FIXTURE = pathlib.Path(__file__).with_name("encrypted_empty.pdf")
ENCRYPTED_SECRET_FIXTURE = pathlib.Path(__file__).with_name("encrypted_secret.pdf")

PADDING = bytes(
    [
        0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41,
        0x64, 0x00, 0x4E, 0x56, 0xFF, 0xFA, 0x01, 0x08,
        0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
        0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
    ]
)


def rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    jump = 0
    for index in range(256):
        jump = (jump + state[index] + key[index % len(key)]) & 0xFF
        state[index], state[jump] = state[jump], state[index]
    output = bytearray()
    first = second = 0
    for byte in data:
        first = (first + 1) & 0xFF
        second = (second + state[first]) & 0xFF
        state[first], state[second] = state[second], state[first]
        output.append(byte ^ state[(state[first] + state[second]) & 0xFF])
    return bytes(output)


def padded(password: str) -> bytes:
    return (password.encode("latin-1") + PADDING)[:32]


def encryption_material(user: str, owner: str, file_id: bytes):
    owner_key = hashlib.md5(padded(owner)).digest()[:5]
    owner_entry = rc4(owner_key, padded(user))
    permissions = (-3904).to_bytes(4, "little", signed=True)
    file_key = hashlib.md5(
        padded(user) + owner_entry + permissions + file_id
    ).digest()[:5]
    user_entry = rc4(file_key, PADDING)
    return owner_entry, user_entry, file_key, permissions


def object_key(file_key: bytes, number: int, generation: int = 0) -> bytes:
    return hashlib.md5(
        file_key + number.to_bytes(3, "little") + generation.to_bytes(2, "little")
    ).digest()[: min(len(file_key) + 5, 16)]


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


def build_streams(
    streams: list[bytes], path: pathlib.Path, *, compress: bool = True
) -> None:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )
    font_times = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman "
        b"/Encoding /WinAnsiEncoding >>"
    )
    pages_id = len(objects) + 1
    add(b"")  # pages root, filled in once the page objects exist
    page_ids = []
    for stream in streams:
        if compress and stream:
            body = zlib.compress(stream)
            head = b"<< /Length %d /Filter /FlateDecode >>" % len(body)
        else:
            body = stream
            head = b"<< /Length %d >>" % len(body)
        contents = add(head + b"\nstream\n" + body + b"\nendstream")
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R "
                    f"/MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font} 0 R /F2 {font_times} 0 R >> >> "
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


def build(pages: list[list[str]], path: pathlib.Path) -> None:
    build_streams([content(lines) for lines in pages], path)


def build_encrypted(
    pages: list[list[str]], path: pathlib.Path, user: str, owner: str
) -> None:
    file_id = hashlib.md5(b"engram-pdf-encrypted-fixture").digest()
    owner_entry, user_entry, file_key, permissions = encryption_material(
        user, owner, file_id
    )
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )
    pages_id = len(objects) + 1
    add(b"")
    page_ids = []
    for lines in pages:
        stream = content(lines)
        body = zlib.compress(stream) if stream else b""
        contents_number = len(objects) + 1
        body = rc4(object_key(file_key, contents_number), body)
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
    encrypt = add(
        (
            f"<< /Filter /Standard /V 1 /R 2 /Length 40 "
            f"/O <{owner_entry.hex()}> /U <{user_entry.hex()}> "
            f"/P {int.from_bytes(permissions, 'little', signed=True)} >>"
        ).encode()
    )

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
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R "
        f"/Encrypt {encrypt} 0 R /ID [<{file_id.hex()}> <{file_id.hex()}>] >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(out)


PAGES = [
    [
        "Quarterly Report",
        "Revenue grew 12% in Café operations across every region this quarter.",
        "R&D (draft) review: naïve façade.",
        "Multiple   spaces   stay single.",
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
    # Corrupt page content: an unterminated string literal. Lenient decoders
    # would silently ingest a partial page; the Rust preflight must reject it.
    build_streams([b"BT /F1 12 Tf (Quarterly Tj ET"], BROKEN_FIXTURE, compress=False)
    build_encrypted(
        [["Encrypted report", "Café secret is readable."]],
        ENCRYPTED_EMPTY_FIXTURE,
        user="",
        owner="owner-secret",
    )
    build_encrypted(
        [["Encrypted report", "Café secret is readable."]],
        ENCRYPTED_SECRET_FIXTURE,
        user="secret",
        owner="owner-secret",
    )
