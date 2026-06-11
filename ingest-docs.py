#!/usr/bin/env python3
"""Ingest kubernaut-docs into Hindsight knowledge bank.

Creates a 'kubernaut-docs' bank with chunks extraction mode (zero LLM cost)
and ingests all markdown files from the kubernaut-docs repository.

Usage:
    python3 ingest-docs.py [--docs-dir PATH] [--hindsight-url URL]
"""

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")
BANK_ID = "kubernaut-docs"
DEFAULT_DOCS_DIR = os.path.expanduser("~/go/src/github.com/jordigilh/kubernaut-docs/docs")


def api_request(method, path, payload=None):
    url = f"{HINDSIGHT_URL}{path}"
    data = json.dumps(payload).encode() if payload else None
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode()[:300] if e.fp else ""
        print(f"  API error {e.code}: {body}", file=sys.stderr)
        raise


def create_bank():
    print(f"Creating bank '{BANK_ID}' with chunks mode...")
    try:
        api_request("PUT", f"/v1/default/banks/{BANK_ID}",
                    {"description": "Kubernaut published documentation (architecture, API, operations)"})
    except HTTPError as e:
        if e.code == 409:
            print(f"  Bank '{BANK_ID}' already exists, skipping creation.")
        else:
            raise

    api_request("PATCH", f"/v1/default/banks/{BANK_ID}/config",
                {"updates": {"retain_extraction_mode": "chunks", "retain_chunk_size": 800}})
    print("  Configured: extraction_mode=chunks, chunk_size=800")


def ingest(docs_dir: Path):
    files = sorted(docs_dir.rglob("*.md"))
    print(f"Ingesting {len(files)} markdown files from {docs_dir}...")

    total_chunks = 0
    errors = 0

    for i, md_file in enumerate(files):
        rel = md_file.relative_to(docs_dir)
        doc_id = str(rel).replace("/", "--").replace(".md", "")
        parts = rel.parts
        section = parts[0] if len(parts) > 1 else "root"

        content = md_file.read_text(encoding="utf-8")
        if not content.strip():
            continue

        payload = {
            "items": [{
                "content": content,
                "document_id": doc_id,
                "timestamp": "unset",
                "tags": [section],
                "metadata": {"source": "kubernaut-docs"},
            }]
        }

        try:
            result = api_request("POST", f"/v1/default/banks/{BANK_ID}/memories", payload)
            count = result.get("items_count", 0)
            total_chunks += count
            if (i + 1) % 10 == 0 or (i + 1) == len(files):
                print(f"  [{i+1}/{len(files)}] {doc_id} -> {count} chunks")
        except Exception as e:
            errors += 1
            print(f"  ERROR {doc_id}: {e}", file=sys.stderr)

    print(f"\nDone: {len(files)} files, {total_chunks} chunks ingested, {errors} errors")
    return errors == 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ingest kubernaut-docs into Hindsight")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR,
                        help="Path to kubernaut-docs/docs/ directory")
    parser.add_argument("--hindsight-url", default=HINDSIGHT_URL,
                        help="Hindsight API URL")
    args = parser.parse_args()

    global HINDSIGHT_URL
    HINDSIGHT_URL = args.hindsight_url

    docs_dir = Path(args.docs_dir)
    if not docs_dir.exists():
        print(f"Error: docs directory not found: {docs_dir}", file=sys.stderr)
        sys.exit(1)

    create_bank()
    success = ingest(docs_dir)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
