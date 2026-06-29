#!/usr/bin/env python3
"""Ingest GitHub issues into Hindsight knowledge bank.

DEPRECATED: This script is superseded by CocoIndex, which provides real-time
incremental ingestion with delta processing (polling every 5 min). The launchd
job (io.vectorize.hindsight.issues.plist) has been disabled. This script remains
for one-time manual use or environments without CocoIndex.

Creates a 'kubernaut-issues' bank with chunks extraction mode (zero LLM cost)
and ingests issues from the kubernaut repository. Filters out bot comments
and CI noise to keep the knowledge signal-rich.

Usage:
    python3 ingest-issues.py                          # open + closed (90 days)
    python3 ingest-issues.py --open-only              # only open issues
    python3 ingest-issues.py --days 180              # closed within 180 days
    python3 ingest-issues.py --repo org/other-repo   # different repo
    python3 ingest-issues.py --refresh               # re-ingest (idempotent)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BANK_ID = "kubernaut-issues"
DEFAULT_REPO = "jordigilh/kubernaut"
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}

_config = {"hindsight_url": os.environ.get("HINDSIGHT_URL", "http://localhost:8888")}


def api_request(method, path, payload=None):
    url = f"{_config['hindsight_url']}{path}"
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
                    {"description": "Kubernaut GitHub issues (decisions, requirements, known bugs)"})
    except HTTPError as e:
        if e.code == 409:
            print(f"  Bank '{BANK_ID}' already exists, skipping creation.")
        else:
            raise

    api_request("PATCH", f"/v1/default/banks/{BANK_ID}/config",
                {"updates": {"retain_extraction_mode": "chunks", "retain_chunk_size": 1200}})
    print("  Configured: extraction_mode=chunks, chunk_size=1200")


def fetch_issues(repo: str, state: str, limit: int = 500) -> list[dict]:
    """Fetch issues from GitHub using gh CLI."""
    fields = "number,title,body,state,labels,createdAt,closedAt,comments,author"
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--state", state,
        "--limit", str(limit),
        "--json", fields,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"  gh CLI error: {result.stderr[:200]}", file=sys.stderr)
        return []
    return json.loads(result.stdout)


def format_issue_content(issue: dict) -> str:
    """Format an issue into a single text document for ingestion.

    Includes title, labels, body, and human comments (filtered).
    """
    parts = []

    number = issue.get("number", "?")
    title = issue.get("title", "")
    state = issue.get("state", "OPEN")
    labels = [l.get("name", "") for l in issue.get("labels", [])]
    author = issue.get("author", {}).get("login", "unknown")
    created = issue.get("createdAt", "")[:10]

    parts.append(f"# Issue #{number}: {title}")
    parts.append(f"State: {state} | Labels: {', '.join(labels) or 'none'} | Author: {author} | Created: {created}")
    parts.append("")

    body = issue.get("body", "") or ""
    if body.strip():
        parts.append(body.strip())
        parts.append("")

    # Include human comments only
    comments = issue.get("comments", []) or []
    human_comments = [
        c for c in comments
        if c.get("authorAssociation", "NONE") in TRUSTED_ASSOCIATIONS
        and not c.get("author", {}).get("login", "").endswith("[bot]")
        and len(c.get("body", "")) > 20
    ]

    if human_comments:
        parts.append("---")
        parts.append(f"## Discussion ({len(human_comments)} comments)")
        parts.append("")
        for c in human_comments[:10]:
            c_author = c.get("author", {}).get("login", "?")
            c_body = c.get("body", "").strip()
            if len(c_body) > 2000:
                c_body = c_body[:2000] + "\n[...truncated]"
            parts.append(f"**@{c_author}:**")
            parts.append(c_body)
            parts.append("")

    return "\n".join(parts)


def ingest_issues(issues: list[dict]) -> tuple[int, int]:
    """Ingest formatted issues into Hindsight bank."""
    ingested = 0
    errors = 0

    for i, issue in enumerate(issues):
        number = issue.get("number", 0)
        content = format_issue_content(issue)

        if not content.strip() or len(content) < 50:
            continue

        labels = [l.get("name", "") for l in issue.get("labels", [])]
        state = issue.get("state", "OPEN").lower()
        tags = [state] + labels[:5]

        payload = {
            "items": [{
                "content": content,
                "document_id": f"issue-{number}",
                "timestamp": issue.get("createdAt", "unset"),
                "tags": tags,
                "metadata": {
                    "source": "github-issues",
                    "issue_number": str(number),
                    "state": state,
                },
            }]
        }

        try:
            api_request("POST", f"/v1/default/banks/{BANK_ID}/memories", payload)
            ingested += 1
            if (i + 1) % 25 == 0 or (i + 1) == len(issues):
                print(f"  [{i+1}/{len(issues)}] #{number} ingested")
        except Exception as e:
            errors += 1
            print(f"  ERROR #{number}: {e}", file=sys.stderr)

    return ingested, errors


def main():
    parser = argparse.ArgumentParser(description="Ingest GitHub issues into Hindsight")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo (default: jordigilh/kubernaut)")
    parser.add_argument("--days", type=int, default=90, help="Include closed issues from last N days (default: 90)")
    parser.add_argument("--open-only", action="store_true", help="Only ingest open issues")
    parser.add_argument("--limit", type=int, default=500, help="Max issues per state to fetch")
    parser.add_argument("--refresh", action="store_true", help="Re-ingest (overwrites existing documents)")
    parser.add_argument("--hindsight-url", default=_config["hindsight_url"], help="Hindsight API URL")
    args = parser.parse_args()

    _config["hindsight_url"] = args.hindsight_url

    create_bank()

    # Fetch open issues
    print(f"\nFetching open issues from {args.repo}...")
    open_issues = fetch_issues(args.repo, "open", limit=args.limit)
    print(f"  Found {len(open_issues)} open issues")

    # Fetch recently closed issues
    closed_issues = []
    if not args.open_only:
        print(f"Fetching closed issues (last {args.days} days)...")
        all_closed = fetch_issues(args.repo, "closed", limit=args.limit)
        cutoff = datetime.now() - timedelta(days=args.days)
        for issue in all_closed:
            closed_at = issue.get("closedAt", "")
            if closed_at:
                try:
                    closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00").replace("+00:00", ""))
                    if closed_dt >= cutoff:
                        closed_issues.append(issue)
                except ValueError:
                    closed_issues.append(issue)
        print(f"  Found {len(closed_issues)} closed issues within {args.days} days")

    all_issues = open_issues + closed_issues
    print(f"\nIngesting {len(all_issues)} issues...")

    ingested, errors = ingest_issues(all_issues)

    print(f"\nDone: {ingested} issues ingested, {errors} errors")
    print(f"  Open: {len(open_issues)}, Closed (recent): {len(closed_issues)}")

    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
