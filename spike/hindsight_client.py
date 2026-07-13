"""Minimal Hindsight recall client for the spike's real-world contradiction
sanity check (querying the actual cursor-memory bank content).

Mirrors the request shape used in nightly-learn.py's measure_recall_quality().
"""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")


def recall(bank: str, query: str, max_results: int = 5, retries: int = 2) -> list[tuple[str, str]]:
    """Return up to max_results (document_id, text) pairs relevant to query.

    Fixed 2026-07-12: this previously parsed a "chunks" key that the live
    hindsight-api response never populates (always {}) -- the real payload
    shape is {"results": [...]}, matching nightly-learn.py's own
    measure_recall_quality(). Confirmed against a live recall that "document_id"
    per result is the same ID accepted by DELETE /v1/default/banks/{bank}/
    documents/{document_id} (see docs/FINDINGS.md).
    """
    import time

    url = f"{HINDSIGHT_URL}/v1/default/banks/{bank}/memories/recall"
    payload = {"query": query, "max_tokens": 2048, "include": {"chunks": {}}}
    result = None
    for attempt in range(retries + 1):
        req = Request(
            url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
            break
        except (HTTPError, URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"  [hindsight_client] recall failed for bank={bank!r} after {retries + 1} attempts: {e}")
            return []

    results_list = result.get("results", [])
    pairs = []
    for r in results_list:
        if isinstance(r, dict) and r.get("text") and r.get("document_id"):
            pairs.append((r["document_id"], r["text"]))
    return pairs[:max_results]
