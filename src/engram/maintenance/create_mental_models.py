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
    # koku-docs: Django/Python cost-management platform architecture + ops
    {
        "bank": "koku-docs",
        "id": "koku-architecture",
        "name": "Koku Architecture",
        "source_query": "How is the Koku cost-management platform structured? Describe its Django app layout, the masu report-processing/cost-model pipeline, Celery task orchestration, and how AWS/Azure/GCP/OCP cost data flows through the system.",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    {
        "bank": "koku-docs",
        "id": "koku-operations",
        "name": "Koku Operations",
        "source_query": "How is Koku deployed and operated? Describe local devtools setup, ephemeral environments, install steps, and release/distribution processes.",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    # koku-issues: requirements/direction (delta, nightly refresh)
    {
        "bank": "koku-issues",
        "id": "active-priorities",
        "name": "Active Priorities",
        "source_query": "What are the current open issues, their priorities, and what direction the project-koku/koku project is heading?",
        "max_tokens": 4096,
        "trigger": {"mode": "delta", "refresh_after_consolidation": False},
    },
    {
        "bank": "koku-issues",
        "id": "known-bugs",
        "name": "Known Bugs and Workarounds",
        "source_query": "What are the known bugs, their root causes, and any workarounds documented in koku issues?",
        "max_tokens": 4096,
        "trigger": {"mode": "delta", "refresh_after_consolidation": False},
    },
    # cursor-memory, tag-isolated (tags=["koku"], strict match) sibling of
    # kubernaut's/dcm's/engram's own scoped models -- same 2026-07-27 fix
    # pattern applied from day one for koku's onboarding, see docs/FINDINGS.md.
    {
        "bank": "cursor-memory",
        "id": "koku-coding-conventions",
        "name": "Koku Coding Conventions",
        "source_query": "What are the user's coding conventions, naming patterns, and style preferences when working on koku (Python/Django)?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
        "tags": ["koku"],
    },
    {
        "bank": "cursor-memory",
        "id": "koku-testing-methodology",
        "name": "Koku Testing Methodology",
        "source_query": "What testing approach, frameworks, and patterns does the user follow when working on koku?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
        "tags": ["koku"],
    },
    {
        "bank": "cursor-memory",
        "id": "koku-workflow-preferences",
        "name": "Koku Development Workflow",
        "source_query": "What is the user's preferred development workflow, review process, and tooling when working on koku?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
        "tags": ["koku"],
    },
    {
        "bank": "cursor-memory",
        "id": "koku-architecture-decisions",
        "name": "Koku Architecture Decisions",
        "source_query": "What architectural decisions and design patterns has the user established while working on koku?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
        "tags": ["koku"],
    },
    # rhdh-plugins-docs: narrow-scope onboarding (2026-08-13) -- only
    # workspaces/boost/ (the package touching the AI Catalog Graduated
    # Visibility Permissions epic), not the full 23-package monorepo. See
    # src/engram/flows/rhdh_plugins.py's module docstring for the full
    # scoping rationale.
    {
        "bank": "rhdh-plugins-docs",
        "id": "rhdh-plugins-ai-catalog-rbac-design",
        "name": "AI Catalog RBAC Design",
        "source_query": "How does the AI Catalog Graduated Visibility Permissions design work in the boost workspace? Describe the ai-catalog.* permissions, per-category/per-connector conditional policies, AiCatalogFilterBlueprint, and SkillBundle RBAC filtering as specified in the boost workspace's specifications and openspec change docs.",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    # rhdh-plugins-issues: requirements/direction (delta, nightly refresh),
    # scoped to Jira epic RHIDP-15270 and its children only.
    {
        "bank": "rhdh-plugins-issues",
        "id": "rhdh-plugins-active-priorities",
        "name": "AI Catalog RBAC Epic Progress",
        "source_query": "What is the status and remaining scope of the AI Catalog Graduated Visibility Permissions epic (RHIDP-15270) and its child stories? What permissions, backend filtering, and frontend gating work remains?",
        "max_tokens": 4096,
        "trigger": {"mode": "delta", "refresh_after_consolidation": False},
    },
    # cursor-memory, tag-isolated (tags=["rhdh-plugins"]) sibling of
    # koku's/kubernaut's/dcm's/engram's own scoped models -- same 2026-07-27
    # fix pattern applied from day one for this onboarding.
    {
        "bank": "cursor-memory",
        "id": "rhdh-plugins-coding-conventions",
        "name": "rhdh-plugins Coding Conventions",
        "source_query": "What are the user's coding conventions, naming patterns, and style preferences when working on rhdh-plugins (TypeScript/React Backstage plugins)?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
        "tags": ["rhdh-plugins"],
    },
    {
        "bank": "cursor-memory",
        "id": "rhdh-plugins-testing-methodology",
        "name": "rhdh-plugins Testing Methodology",
        "source_query": "What testing approach, frameworks, and patterns does the user follow when working on rhdh-plugins?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
        "tags": ["rhdh-plugins"],
    },
    {
        "bank": "cursor-memory",
        "id": "rhdh-plugins-workflow-preferences",
        "name": "rhdh-plugins Development Workflow",
        "source_query": "What is the user's preferred development workflow, review process, and tooling when working on rhdh-plugins?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
        "tags": ["rhdh-plugins"],
    },
    {
        "bank": "cursor-memory",
        "id": "rhdh-plugins-architecture-decisions",
        "name": "rhdh-plugins Architecture Decisions",
        "source_query": "What architectural decisions and design patterns has the user established while working on rhdh-plugins?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
        "tags": ["rhdh-plugins"],
    },
    # praxis-docs: Rust AI gateway/grid architecture, RFC-style design discussions
    {
        "bank": "praxis-docs",
        "id": "praxis-architecture",
        "name": "Praxis Architecture",
        "source_query": "How is Praxis (the AI gateway proxy) and Praxis Grid (multi-cluster/multi-site control plane) structured? Describe the filter chain model, the Grid Operator overlay-rendering pipeline, routing-overlay contract, scoring/admission, and CRDT/SWIM state propagation between sites.",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    {
        "bank": "praxis-docs",
        "id": "praxis-enhancements",
        "name": "Praxis Enhancement Proposals and Design Debates",
        "source_query": "What enhancement proposals, RFC-style design debates, and architectural trade-offs have been discussed for Praxis routing, filters, or Grid?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    {
        "bank": "praxis-docs",
        "id": "praxis-api-contracts",
        "name": "Praxis API and CRD Contracts",
        "source_query": "What are the API contracts, CRD schemas (GridNetwork, InferenceProvider, GridSite), and routing-overlay envelope formats used by Praxis and Grid?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    # praxis-issues: requirements/direction (delta, nightly refresh)
    {
        "bank": "praxis-issues",
        "id": "active-priorities",
        "name": "Active Priorities",
        "source_query": "What are the current open issues, their priorities, and what direction the praxis-proxy org is heading?",
        "max_tokens": 4096,
        "trigger": {"mode": "delta", "refresh_after_consolidation": False},
    },
    {
        "bank": "praxis-issues",
        "id": "known-bugs",
        "name": "Known Bugs and Workarounds",
        "source_query": "What are the known bugs, their root causes, and any workarounds documented in praxis-proxy issues?",
        "max_tokens": 4096,
        "trigger": {"mode": "delta", "refresh_after_consolidation": False},
    },
    {
        # Answers "what ships first" questions directly from ingested GitHub
        # Projects (v2) board Status fields + issue milestones, rather than
        # requiring a manual `gh api graphql` investigation each time -- see
        # docs/findings/2026-08.md's 2026-08-10 praxis-proxy roadmap-signal
        # entry for the investigation that motivated this model.
        "bank": "praxis-issues",
        "id": "praxis-roadmap-priorities",
        "name": "Praxis Roadmap Priorities",
        "source_query": "Which praxis-proxy initiatives are actively staffed and prioritized right now (GitHub Project board Status: Next/In Progress vs Backlog/Epics with no board placement), and what are their milestone due dates? Cover multi-cluster/model routing, the AI Grid board, mixture-of-models/intelligent routing, and semantic routing specifically.",
        "max_tokens": 4096,
        "trigger": {"mode": "delta", "refresh_after_consolidation": False},
    },
    # cursor-memory, tag-isolated (tags=["praxis"], strict match) sibling of
    # kubernaut's/dcm's/engram's/koku's own scoped models -- same 2026-07-27
    # fix pattern applied from day one, see docs/FINDINGS.md.
    {
        "bank": "cursor-memory",
        "id": "praxis-coding-conventions",
        "name": "Praxis Coding Conventions",
        "source_query": "What are the user's coding conventions, naming patterns, and style preferences when working on praxis-proxy (Rust)?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
        "tags": ["praxis"],
    },
    {
        "bank": "cursor-memory",
        "id": "praxis-testing-methodology",
        "name": "Praxis Testing Methodology",
        "source_query": "What testing approach, frameworks, and patterns does the user follow when working on praxis-proxy?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
        "tags": ["praxis"],
    },
    {
        "bank": "cursor-memory",
        "id": "praxis-workflow-preferences",
        "name": "Praxis Development Workflow",
        "source_query": "What is the user's preferred development workflow, review process, and tooling when working on praxis-proxy?",
        "max_tokens": 2048,
        "trigger": {"mode": "delta", "refresh_after_consolidation": True},
        "tags": ["praxis"],
    },
    {
        "bank": "cursor-memory",
        "id": "praxis-architecture-decisions",
        "name": "Praxis Architecture Decisions",
        "source_query": "What architectural decisions and design patterns has the user established while working on praxis-proxy?",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
        "tags": ["praxis"],
    },
    # kuadrant-docs/kuadrant-issues: ingestion-only prior-art reference for
    # praxis-proxy (2026-08-27 onboarding) -- see engram.flows.kuadrant's
    # module docstring. No cursor-memory entry: nobody develops against
    # these 8 read-only reference checkouts, so there's no coding-
    # conventions/workflow-preferences signal to extract a model from.
    {
        "bank": "kuadrant-docs",
        "id": "kuadrant-architecture",
        "name": "Kuadrant Architecture",
        "source_query": "How is Kuadrant (the multi-cluster API/AI gateway policy suite) structured? Describe Authorino's external authorization model, Limitador's rate limiting, wasm-shim's Envoy WASM filter integration, DNSPolicy/TLSPolicy, and the AuthPolicy/RateLimitPolicy/DNSPolicy policy-attachment pattern via the Kubernetes Gateway API.",
        "max_tokens": 4096,
        "trigger": {"mode": "full", "refresh_after_consolidation": False},
    },
    {
        "bank": "kuadrant-issues",
        "id": "kuadrant-active-priorities",
        "name": "Kuadrant Active Priorities",
        "source_query": "What are the current open issues, design decisions, and direction across the Kuadrant org (kuadrant-operator, limitador, authorino, wasm-shim, dns-operator)?",
        "max_tokens": 4096,
        "trigger": {"mode": "delta", "refresh_after_consolidation": False},
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
    banks = ["cursor-memory", "kubernaut-docs", "kubernaut-issues", "dcm-docs", "dcm-issues", "engram-docs", "koku-docs", "koku-issues", "praxis-docs", "praxis-issues", "rhdh-plugins-docs", "rhdh-plugins-issues", "kuadrant-docs", "kuadrant-issues"]
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
