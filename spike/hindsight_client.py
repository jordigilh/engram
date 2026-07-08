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


def recall(bank: str, query: str, max_results: int = 5, retries: int = 2) -> list[str]:
    """Return up to max_results memory text snippets relevant to query."""
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

    chunks = result.get("chunks", {})
    texts = []
    if isinstance(chunks, dict):
        for c in chunks.values():
            if isinstance(c, dict) and c.get("text"):
                texts.append(c["text"])
    return texts[:max_results]
