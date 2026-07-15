#!/usr/bin/env python3
"""Create and refresh mental models across all Hindsight banks.

Mental models are persistent, LLM-synthesized documents that sit above raw
facts in the recall hierarchy. They provide pre-synthesized context blocks
instead of scattered individual facts, reducing token cost and improving
accuracy during agent sessions.

Usage:
    python3 create-mental-models.py              # create + refresh all
    python3 create-mental-models.py --list       # show existing models
    python3 create-mental-models.py --refresh    # refresh existing (no create)
"""

import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_config = {"hindsight_url": os.environ.get("HINDSIGHT_URL", "http://localhost:8888")}

MENTAL_MODELS = [
    # cursor-memory: behavioral patterns (delta, auto-refresh after consolidation)
    {
        "bank": "cursor-memory",
        "id": "coding-conventions",
        "name": "Coding Conventions",
        "source_query": "What are the user's coding conventions, naming patterns, and style preferences?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
    },
    {
        "bank": "cursor-memory",
        "id": "testing-methodology",
        "name": "Testing Methodology",
        "source_query": "What testing approach, frameworks, and patterns does the user follow?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
    },
    {
        "bank": "cursor-memory",
        "id": "workflow-preferences",
        "name": "Development Workflow",
        "source_query": "What is the user's preferred development workflow, review process, and tooling?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
    },
    {
        "bank": "cursor-memory",
        "id": "architecture-decisions",
        "name": "Architecture Decisions",
        "source_query": "What architectural decisions and design patterns has the user established?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    # kubernaut-docs: technical knowledge (full, manual refresh)
    {
        "bank": "kubernaut-docs",
        "id": "ka-architecture",
        "name": "KA Service Architecture",
        "source_query": "How does the KA (Kubernaut Agent) service work? What are its main components, data flow, and integration points?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    {
        "bank": "kubernaut-docs",
        "id": "af-pipeline",
        "name": "AF Pipeline Architecture",
        "source_query": "How does the AF (Autonomous Framework) pipeline work? What are the stages, event flow, and decision points?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    {
        "bank": "kubernaut-docs",
        "id": "platform-topology",
        "name": "Platform Topology",
        "source_query": "What services make up the kubernaut platform, how do they interact, and what infrastructure do they run on?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    # kubernaut-issues: requirements/direction (delta, nightly refresh)
    {
        "bank": "kubernaut-issues",
        "id": "active-priorities",
        "name": "Active Priorities",
        "source_query": "What are the current open issues, their priorities, and what direction is the platform heading?",
        "max_tokens": 4096,
        "trigger": {"mode": "delta", "refresh_after_consolidation": False},
    },
    {
        "bank": "kubernaut-issues",
        "id": "known-bugs",
        "name": "Known Bugs and Workarounds",
        "source_query": "What are the known bugs, their root causes, and any workarounds documented in issues?",
        "max_tokens": 4096,
        "trigger": {"mode": "delta", "refresh_after_consolidation": False},
    },
    # kubernaut-docs, tag-scoped: narrow per-repo views on top of the shared
    # bank, so kubernaut-operator/kubernaut-console get a focused model
    # without needing their own dedicated bank. See docs/FINDINGS.md.
    {
        "bank": "kubernaut-docs",
        "id": "operator-architecture",
        "name": "Kubernaut Operator Architecture",
        "source_query": "What is the architecture of the kubernaut-operator service -- its CRDs, controllers, reconciliation loops, and how it integrates with the rest of the kubernaut platform?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
        "tags": ["kubernaut-operator"],
    },
    {
        "bank": "kubernaut-docs",
        "id": "console-architecture",
        "name": "Kubernaut Console Architecture",
        "source_query": "What is the architecture of the kubernaut console plugin -- its components, how it communicates with platform APIs, and its UI design?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
        "tags": ["kubernaut-console"],
    },
    # engram-docs: this repo's own Hindsight + CocoIndex tooling
    {
        "bank": "engram-docs",
        "id": "engram-architecture",
        "name": "Engram Pipeline Architecture",
        "source_query": "How does the Engram Hindsight + CocoIndex pipeline work? Describe nightly-learn.py, cocoindex-flows.py, the Haiku correction gate, the three-tier contradiction resolution system, and how project scoping isolates ingestion per Cursor workspace.",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    {
        "bank": "engram-docs",
        "id": "engram-operations",
        "name": "Engram Operations",
        "source_query": "How is Engram deployed and operated? Describe the launchd services, the ~/.hindsight/ symlink layout, the Python venv setup, and the pytest regression test suite.",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
]


def api_request(method, path, payload=None):
    url = f"{_config['hindsight_url']}{path}"
    data = json.dumps(payload).encode() if payload else None
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode()[:300] if e.fp else ""
        return {"error": e.code, "detail": body}


def create_model(model: dict) -> bool:
    bank = model["bank"]
    payload = {
        "id": model["id"],
        "name": model["name"],
        "source_query": model["source_query"],
        "max_tokens": model["max_tokens"],
        "trigger": model["trigger"],
    }
    if model.get("tags"):
        payload["tags"] = model["tags"]
    result = api_request("POST", f"/v1/default/banks/{bank}/mental-models", payload)
    if "error" in result:
        if result["error"] == 409:
            print(f"  [{bank}] {model['id']}: already exists")
            return True
        print(f"  [{bank}] {model['id']}: FAILED ({result})", file=sys.stderr)
        return False
    print(f"  [{bank}] {model['id']}: created")
    return True


def refresh_model(bank: str, model_id: str) -> bool:
    result = api_request("POST", f"/v1/default/banks/{bank}/mental-models/{model_id}/refresh")
    if "error" in result:
        print(f"  [{bank}] {model_id}: refresh FAILED ({result})", file=sys.stderr)
        return False
    print(f"  [{bank}] {model_id}: refresh triggered")
    return True


def list_models():
    banks = ["cursor-memory", "kubernaut-docs", "kubernaut-issues", "dcm-docs", "dcm-issues", "engram-docs"]
    for bank in banks:
        result = api_request("GET", f"/v1/default/banks/{bank}/mental-models")
        items = result.get("items", [])
        if items:
            print(f"\n{bank} ({len(items)} models):")
            for m in items:
                content_len = len(m.get("content", "") or "")
                refreshed = m.get("last_refreshed_at", "never")[:19] if m.get("last_refreshed_at") else "never"
                print(f"  {m['id']:25s} content={content_len:5d} chars  refreshed={refreshed}")
        else:
            print(f"\n{bank}: no mental models")


def wait_for_refresh(banks_models: list[tuple[str, str]], timeout: int = 300):
    """Poll until all models have content or timeout."""
    start = time.time()
    pending = set(banks_models)

    while pending and (time.time() - start) < timeout:
        time.sleep(10)
        still_pending = set()
        for bank, model_id in pending:
            result = api_request("GET", f"/v1/default/banks/{bank}/mental-models/{model_id}")
            content = result.get("content", "") or ""
            if len(content) > 50:
                print(f"  [{bank}] {model_id}: ready ({len(content)} chars)")
            else:
                still_pending.add((bank, model_id))
        pending = still_pending
        if pending:
            elapsed = int(time.time() - start)
            print(f"  ... waiting ({elapsed}s, {len(pending)} pending)")

    if pending:
        print(f"\n  WARNING: {len(pending)} models still pending after {timeout}s:")
        for bank, model_id in pending:
            print(f"    [{bank}] {model_id}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Create and refresh Hindsight mental models")
    parser.add_argument("--list", action="store_true", help="List existing mental models")
    parser.add_argument("--refresh", action="store_true", help="Refresh existing models (skip creation)")
    parser.add_argument("--no-wait", action="store_true", help="Don't wait for refresh to complete")
    parser.add_argument("--hindsight-url", default=_config["hindsight_url"], help="Hindsight API URL")
    args = parser.parse_args()

    _config["hindsight_url"] = args.hindsight_url

    if args.list:
        list_models()
        return

    if args.refresh:
        print("Refreshing existing mental models...")
        for model in MENTAL_MODELS:
            refresh_model(model["bank"], model["id"])
        if not args.no_wait:
            print("\nWaiting for refreshes to complete...")
            pairs = [(m["bank"], m["id"]) for m in MENTAL_MODELS]
            wait_for_refresh(pairs)
        return

    # Create all models
    print("Creating mental models...")
    created = []
    for model in MENTAL_MODELS:
        if create_model(model):
            created.append((model["bank"], model["id"]))

    # Trigger initial refresh
    print(f"\nTriggering initial refresh for {len(created)} models...")
    refreshed = []
    for bank, model_id in created:
        if refresh_model(bank, model_id):
            refreshed.append((bank, model_id))

    if not args.no_wait and refreshed:
        print(f"\nWaiting for {len(refreshed)} refreshes to complete...")
        success = wait_for_refresh(refreshed, timeout=600)
        if success:
            print("\nAll mental models created and populated.")
        else:
            print("\nSome models still refreshing. Run --list to check status.")
    else:
        print(f"\nRefresh triggered for {len(refreshed)} models. Run --list to check status.")


if __name__ == "__main__":
    main()
