#!/usr/bin/env python3
"""Preflight smoke test for the Semantic Correction Detection Spike.

Confirms that litellm can reach vertex_ai/claude-haiku-4-5 (and, separately,
claude-sonnet-4-6 for the contradiction-check config) from a standalone
script using the same GOOGLE_APPLICATION_CREDENTIALS/VERTEXAI_PROJECT setup
that hindsight-api itself uses -- but invoked directly, outside of
hindsight-api's own process. This is step 0 of the spike plan: resolve the
auth-scoping unknown before investing in the rest of the pipeline.

Run with the hindsight venv (has litellm/vertexai installed):
    ~/.hindsight/venv/bin/python3 spike/preflight_smoke_test.py

Exits non-zero with a clear message on failure.
"""
from __future__ import annotations

import os
import sys
import time

# Placeholders: set the real VERTEXAI_PROJECT/GOOGLE_CLOUD_PROJECT/
# VERTEXAI_LOCATION in your shell environment -- setdefault() only applies
# these when they're not already set, so a real exported value always wins.
os.environ.setdefault("VERTEXAI_PROJECT", "example-gcp-project")
os.environ.setdefault("VERTEXAI_LOCATION", "us-central1")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "example-gcp-project")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.expanduser("~/.config/gcloud/application_default_credentials.json"),
)

MODELS_TO_TEST = [
    ("haiku", "vertex_ai/claude-haiku-4-5@20251001"),
    ("sonnet", "vertex_ai/claude-sonnet-4-6"),
]


def main() -> int:
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    if not os.path.exists(creds_path):
        print(f"FAIL: GOOGLE_APPLICATION_CREDENTIALS not found at {creds_path}")
        return 1

    try:
        import litellm
    except ImportError as e:
        print(f"FAIL: litellm not importable: {e}")
        return 1

    litellm.vertex_project = os.environ["VERTEXAI_PROJECT"]
    litellm.vertex_location = os.environ["VERTEXAI_LOCATION"]

    overall_ok = True
    for label, model in MODELS_TO_TEST:
        print(f"\n=== Testing {label} ({model}) ===")
        t0 = time.time()
        try:
            resp = litellm.completion(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Reply with exactly one word: OK"
                        ),
                    }
                ],
                max_tokens=10,
                timeout=30,
            )
            elapsed = time.time() - t0
            text = resp.choices[0].message.content
            usage = getattr(resp, "usage", None)
            print(f"  OK  response={text!r}  elapsed={elapsed:.2f}s  usage={usage}")
        except Exception as e:
            overall_ok = False
            print(f"  FAIL: {type(e).__name__}: {e}")

    print()
    if overall_ok:
        print("PREFLIGHT PASSED: litellm/Vertex auth works standalone for both models.")
        return 0
    else:
        print("PREFLIGHT FAILED: see errors above. Do not proceed with the spike build")
        print("until this is resolved (or the plan's model/provider choice is revisited).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
