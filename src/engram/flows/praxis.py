#!/usr/bin/env python3
"""CocoIndex flows for Praxis (the praxis-proxy GitHub org) — incremental
ingestion into Hindsight and pgvector.

Declares three apps:
  1. praxis-docs:   Markdown docs from all praxis-proxy repos -> Hindsight
                     praxis-docs bank
  2. praxis-issues: GitHub issues/PRs, Discussions, and org Project (v2)
                     board status from all praxis-proxy repos -> Hindsight
                     praxis-issues bank
  3. praxis-code:   Rust source from the Rust repos -> pgvector
                     praxis_code_embeddings table

Runs as a single long-lived process via launchd. Supports backfill and live
modes.

Praxis-specific addition over the dcm/koku reference pattern (see
docs/findings/2026-08.md's 2026-08-10 entry, and this project's onboarding
plan's "Roadmap-signal scope" note): a live investigation into "which
initiative should we work first" found that this org's actual prioritization
signal does not live in issue bodies/labels alone -- it lives in GitHub
Projects (v2) board Status fields (Epics/Backlog/Next/In Progress/Review/Done)
and in GitHub Discussions (real design debate, including valuable external
input that a plain issues-only ingest would miss). Both are ingested here in
addition to the standard issues+PRs fetch, feeding the same praxis-issues bank.
"""

import argparse
import dataclasses
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, AsyncIterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import cocoindex as coco
from cocoindex.connectors import localfs
from cocoindex.resources.file import PatternFilePathMatcher

# This file is part of the engram.flows package (src/engram/flows/).
# sys.path[0] for a script invoked via a symlink (as launchd does) resolves
# to the symlink's realpath target directory (src/engram/flows/), not the
# symlink's own directory -- src/ itself must still be added explicitly so
# `engram` resolves as a top-level package rather than needing this file to
# be run via `-m`/an installed console script (not yet true in this repo).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))
from engram import chunking  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("praxis-cocoindex-flows")

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")

PRAXIS_ORG = "praxis-proxy"
PRAXIS_ORG_DIR = pathlib.Path(os.environ.get(
    "PRAXIS_ORG_DIR",
    os.path.expanduser("~/go/src/github.com/praxis-proxy"),
))

# Manually-curated supplementary docs (e.g. project-overview PDFs) that don't
# live in any repo checkout -- distinct from PRAXIS_ORG_DIR, which is
# git-worktree mirrors only. Ingested by process_pdf_file via docs_main,
# tagged source_tag="praxis-manual-docs" so they're identifiable/filterable
# separately from repo docs.
PRAXIS_MANUAL_DOCS_DIR = pathlib.Path(os.environ.get(
    "PRAXIS_MANUAL_DOCS_DIR",
    os.path.expanduser("~/.hindsight/manual-docs/praxis"),
))

# Single source of truth for docs/code/issues ingestion: (local checkout dir
# name, upstream "org/repo" name, has Rust code). pingora is deliberately
# excluded (vendored cloudflare/pingora fork, not team-authored architecture
# -- see the onboarding plan's "Confirmed scope" section).
PRAXIS_REPOS: list[tuple[str, str, bool]] = [
    ("praxis", "praxis-proxy/praxis", True),
    ("praxis-ai", "praxis-proxy/ai", True),
    ("praxis-conventions", "praxis-proxy/conventions", False),
    ("praxis-demos", "praxis-proxy/demos", True),
    ("praxis-enhancements", "praxis-proxy/enhancements", False),
    ("praxis-experiments", "praxis-proxy/experiments", False),
    ("praxis-forge", "praxis-proxy/forge", True),
    ("praxis-grid", "praxis-proxy/grid", True),
    ("praxis-operator", "praxis-proxy/operator", True),
    ("praxis-policy", "praxis-proxy/policy", True),
    ("praxis-proxy.github.io", "praxis-proxy/praxis-proxy.github.io", False),
]

ISSUES_REPOS = os.environ.get(
    "PRAXIS_ISSUES_REPOS",
    ",".join(upstream for _, upstream, _ in PRAXIS_REPOS),
).split(",")
ISSUES_POLL_INTERVAL = int(os.environ.get("PRAXIS_ISSUES_POLL_SECONDS", "300"))

# Known org Project (v2) board numbers, refreshed at startup via
# _fetch_org_projects() -- this default list is just a fallback if that
# discovery call fails, not the source of truth.
_KNOWN_PROJECT_NUMBERS = [2, 3, 4, 5, 7, 8]

PG_DSN = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
# See cocoindex-flows.py's PG_POOL_MIN_SIZE/MAX_SIZE comment (docs/FINDINGS.md
# 2026-08-03) -- asyncpg's own min_size=10/max_size=10 default is oversized
# for this pool's light, bursty pgvector-upsert-only workload.
PG_POOL_MIN_SIZE = int(os.environ.get("COCOINDEX_PG_POOL_MIN_SIZE", "2"))
PG_POOL_MAX_SIZE = int(os.environ.get("COCOINDEX_PG_POOL_MAX_SIZE", "5"))
COCOINDEX_DB = pathlib.Path(os.environ.get(
    "COCOINDEX_DB",
    os.path.expanduser("~/.hindsight/praxis-cocoindex.db"),
))

# Unique per-file ContextKey name -- see engram-cocoindex-flows.py's PG_POOL
# comment / dcm-cocoindex-flows.py's PG_POOL comment for the full rationale.
PG_POOL: coco.ContextKey[Any] = coco.ContextKey("praxis_repo_pg_pool")

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _wait_for_hindsight(max_retries: int = 30, delay: float = 2.0) -> None:
    for attempt in range(max_retries):
        try:
            req = Request(f"{HINDSIGHT_URL}/health", method="GET")
            with urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log.info("Hindsight API healthy at %s", HINDSIGHT_URL)
                    return
        except (HTTPError, URLError, OSError):
            wait = min(delay * (1.5 ** attempt), 30)
            log.warning(
                "Hindsight not ready (attempt %d/%d), retrying in %.1fs...",
                attempt + 1, max_retries, wait,
            )
            time.sleep(wait)
    log.error("Hindsight not reachable after %d attempts, proceeding anyway", max_retries)


def hindsight_retain(
    bank_id: str,
    content: str,
    document_id: str,
    timestamp: str | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    url = f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/memories"
    item: dict[str, Any] = {
        "content": content,
        "document_id": document_id,
    }
    if timestamp:
        item["timestamp"] = timestamp
    if metadata:
        item["metadata"] = metadata
    if tags:
        item["tags"] = tags

    payload = {"items": [item]}
    data = json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})

    for attempt in range(3):
        try:
            with urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        # TimeoutError/ConnectionError added 2026-08-10 -- a slow hindsight-api
        # under retain-consolidation load raises a raw socket TimeoutError
        # (urllib only wraps connect-time failures as URLError; a read-time
        # stall surfaces as bare TimeoutError), which this retry loop's
        # original (HTTPError, URLError) clause didn't catch -- so instead of
        # retrying, it propagated uncaught and crashed the whole backfill
        # process (confirmed live: killed an `issues` app backfill outright).
        except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            log.error("hindsight_retain failed for %s/%s: %s", bank_id, document_id, e)
            return {}


def _repo_short_name(repo: str) -> str:
    return repo.split("/")[-1] if "/" in repo else repo


def _gh_graphql(query: str, **variables: Any) -> dict:
    """Run a `gh api graphql` query with typed variables (via -F, which
    auto-detects int/bool vs. string -- verified empirically 2026-08-10:
    `-F number=3` against an `Int!` variable works correctly, unlike -f
    which always sends a raw string)."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        cmd += ["-F", f"{key}={value}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log.error("gh api graphql failed: %s", result.stderr[:300])
            return {}
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        log.error("_gh_graphql error: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Lifespan: configure CocoIndex database path + Postgres pool for code index
# ---------------------------------------------------------------------------

@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    from cocoindex.connectors import postgres

    builder.settings.db_path = COCOINDEX_DB
    pool = await postgres.create_pool(PG_DSN, min_size=PG_POOL_MIN_SIZE, max_size=PG_POOL_MAX_SIZE)
    builder.provide(PG_POOL, pool)
    yield
    pool.close()


# ---------------------------------------------------------------------------
# App 1: praxis-docs — Markdown docs → Hindsight praxis-docs bank
# ---------------------------------------------------------------------------

def _retain_doc_sections(
    content: str,
    base_doc_id: str,
    section: str,
    source_tag: str,
    timestamp: str | None,
) -> None:
    """Shared chunk+retain tail for praxis-docs, used by both
    process_doc_file (markdown) and process_pdf_file (manually-curated
    PDFs, see PRAXIS_MANUAL_DOCS_DIR). PDFs have no markdown headings, so
    split_markdown_sections() automatically falls through to its
    _numbered_fixed_window() path (stable numbered chunk keys) -- no
    PDF-specific chunking logic needed, just a different route to
    `content`."""
    sections = chunking.split_markdown_sections(content, chunk_size=800, chunk_overlap=200)
    for key, chunk in sections:
        doc_id = base_doc_id if not key else f"{base_doc_id}--{key}"
        hindsight_retain(
            bank_id="praxis-docs",
            content=chunk,
            document_id=doc_id,
            timestamp=timestamp,
            metadata={"source": "cocoindex", "repo": source_tag},
            tags=[section, source_tag],
        )


@coco.fn(memo=True)
async def process_doc_file(
    file: localfs.File,
    base_dir: pathlib.Path,
    source_tag: str,
) -> None:
    content = await file.read_text()
    if not content or not content.strip():
        return

    abs_path = str(file.file_path.resolve())
    base_prefix = str(base_dir) + "/"
    rel_path = abs_path.replace(base_prefix, "") if abs_path.startswith(base_prefix) else file.file_path.name

    try:
        mtime = os.path.getmtime(abs_path)
        timestamp = datetime.fromtimestamp(mtime).isoformat()
    except OSError:
        timestamp = None

    parts = pathlib.Path(rel_path).parts
    section = parts[0] if len(parts) > 1 else "root"

    base_doc_id = f"{source_tag}--{rel_path.replace('/', '--').replace('.md', '')}"
    _retain_doc_sections(content, base_doc_id, section, source_tag, timestamp)


@coco.fn(memo=True)
async def process_pdf_file(
    file: localfs.File,
    base_dir: pathlib.Path,
    source_tag: str,
) -> None:
    """Extract text from a manually-curated PDF (see PRAXIS_MANUAL_DOCS_DIR)
    and retain it into praxis-docs via the same chunk+retain path as
    process_doc_file. Opens the file directly off disk via its resolved
    path rather than through localfs.File.read_text() -- pdfplumber needs
    real file-object/path access for its own PDF parsing, and everything
    under PRAXIS_MANUAL_DOCS_DIR is always a genuine local path (not a
    virtualized/remote source), so this is safe.
    """
    import pdfplumber

    abs_path = str(file.file_path.resolve())
    base_prefix = str(base_dir) + "/"
    rel_path = abs_path.replace(base_prefix, "") if abs_path.startswith(base_prefix) else file.file_path.name

    with pdfplumber.open(abs_path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    content = "\n\n".join(
        f"--- Page {i + 1} ---\n\n{text}" for i, text in enumerate(pages) if text.strip()
    )
    if not content.strip():
        return

    try:
        mtime = os.path.getmtime(abs_path)
        timestamp = datetime.fromtimestamp(mtime).isoformat()
    except OSError:
        timestamp = None

    parts = pathlib.Path(rel_path).parts
    section = parts[0] if len(parts) > 1 else "root"

    base_doc_id = f"{source_tag}--{rel_path.replace('/', '--').replace('.pdf', '')}"
    _retain_doc_sections(content, base_doc_id, section, source_tag, timestamp)


# Homogeneous doc layout across every praxis-proxy repo (unlike DCM's varied
# per-repo structure): docs/**/*.md plus a handful of root-level files.
# proposals/**/*.md added for praxis-enhancements -- that repo's actual
# content (KEP-style enhancement proposals) lives there, not under docs/,
# which only has process/status pages. No-op for every other repo, none of
# which has a proposals/ directory.
_DOC_INCLUDE_PATTERNS = [
    "docs/**/*.md",
    "proposals/**/*.md",
    "README.md",
    ".claude/CLAUDE.md",
    "MAINTAINERS.md",
    "GOVERNANCE.md",
    "CONTRIBUTING.md",
]


@coco.fn
async def docs_main(org_dir: pathlib.Path, manual_docs_dir: pathlib.Path) -> None:
    for local_dir, _upstream, _has_rust in PRAXIS_REPOS:
        repo_dir = org_dir / local_dir
        docs = localfs.walk_dir(
            repo_dir,
            recursive=True,
            path_matcher=PatternFilePathMatcher(included_patterns=_DOC_INCLUDE_PATTERNS),
            live=True,
        )
        await coco.mount_each(
            coco.component_subpath(local_dir),
            process_doc_file, docs.items(),
            repo_dir, local_dir,
        )

    # Manually-curated supplementary docs (e.g. project-overview PDFs) that
    # don't live in any repo checkout -- see PRAXIS_MANUAL_DOCS_DIR comment.
    manual_pdfs = localfs.walk_dir(
        manual_docs_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*.pdf"]),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("praxis-manual-docs"),
        process_pdf_file, manual_pdfs.items(),
        manual_docs_dir, "praxis-manual-docs",
    )


docs_app = coco.App(
    "praxis-docs", docs_main,
    org_dir=PRAXIS_ORG_DIR, manual_docs_dir=PRAXIS_MANUAL_DOCS_DIR,
)


# ---------------------------------------------------------------------------
# App 2: praxis-issues — Issues/PRs + Discussions + Project(v2) board status
#         → Hindsight praxis-issues bank
# ---------------------------------------------------------------------------

def _fetch_all_issues(repo: str) -> list[dict]:
    # "milestone" added over the dcm/koku reference fetch -- see module
    # docstring; cheap addition, meaningfully improves "what's aimed at
    # when" signal (confirmed live: ai#74 targets milestone v0.2.0, due
    # 2026-08-06 -- already past due at ingestion time, itself a signal).
    fields = "number,title,body,state,labels,milestone,createdAt,updatedAt,comments,author"
    all_items: list[dict] = []

    for kind, cmd_base in [("issue", ["gh", "issue", "list"]), ("pr", ["gh", "pr", "list"])]:
        cmd = cmd_base + [
            "--repo", repo,
            "--state", "all",
            "--limit", "10000",
            "--json", fields,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                log.error("gh %s list failed: %s", kind, result.stderr[:300])
                continue
            batch = json.loads(result.stdout)
            for item in batch:
                item["_kind"] = kind
            all_items.extend(batch)
            log.info("Fetched %d %ss from %s", len(batch), kind, repo)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
            log.error("_fetch_all_issues %s error: %s", kind, e)

    return all_items


def _format_issue_header(issue: dict, repo: str) -> str:
    """Static (non-volatile) header. Deliberately excludes state/labels/
    milestone -- those change routinely and are already carried in
    metadata/tags. See dcm-cocoindex-flows.py's docstring / docs/FINDINGS.md
    2026-08-03."""
    number = issue.get("number", "?")
    title = issue.get("title", "")
    kind = issue.get("_kind", "issue")
    kind_label = "PR" if kind == "pr" else "Issue"
    author = issue.get("author", {}).get("login", "unknown")
    created = issue.get("createdAt", "")[:10]
    short_repo = _repo_short_name(repo)

    parts = [
        f"# {kind_label} #{number} ({short_repo}): {title}",
        f"Repo: {repo} | Author: {author} | Created: {created}",
        "",
    ]
    body = issue.get("body", "") or ""
    if body.strip():
        parts.append(body.strip())
    return "\n".join(parts)


def _filter_human_comments(issue: dict) -> list[dict]:
    comments = issue.get("comments", []) or []
    return [
        c for c in comments
        if c.get("authorAssociation", "NONE") in TRUSTED_ASSOCIATIONS
        and not c.get("author", {}).get("login", "").endswith("[bot]")
        and len(c.get("body", "")) > 20
    ][:10]


def _format_comment(comment: dict) -> str:
    c_author = comment.get("author", {}).get("login", "?")
    c_body = comment.get("body", "").strip()
    if len(c_body) > 2000:
        c_body = c_body[:2000] + "\n[...truncated]"
    return f"**@{c_author}:**\n{c_body}"


@coco.fn(memo=True)
def process_issue(issue: dict, repo: str) -> None:
    header = _format_issue_header(issue, repo)
    if not header.strip() or len(header) < 50:
        return

    number = issue.get("number", 0)
    updated_at = issue.get("updatedAt", "")
    state = issue.get("state", "OPEN").lower()
    kind = issue.get("_kind", "issue")
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    milestone = issue.get("milestone") or {}
    milestone_title = milestone.get("title", "")
    short_repo = _repo_short_name(repo)

    comment_texts = [_format_comment(c) for c in _filter_human_comments(issue)]
    base_doc_id = f"{short_repo}-{kind}-{number}"
    sections = chunking.split_issue_sections(header, comment_texts, chunk_size=1200, chunk_overlap=300)

    tags = [state, kind, short_repo] + labels[:5]
    if milestone_title:
        tags.append(f"milestone:{milestone_title}")

    metadata = {
        "source": "cocoindex",
        "repo": repo,
        "kind": kind,
        "number": str(number),
        "state": state,
    }
    if milestone_title:
        metadata["milestone"] = milestone_title
        metadata["milestone_due"] = milestone.get("dueOn", "") or ""

    for key, chunk in sections:
        doc_id = base_doc_id if not key else f"{base_doc_id}-{key}"
        hindsight_retain(
            bank_id="praxis-issues",
            content=chunk,
            document_id=doc_id,
            timestamp=updated_at,
            metadata=metadata,
            tags=tags,
        )


@coco.fn
def issues_main(repos: str) -> None:
    for repo in repos.split(","):
        repo = repo.strip()
        if not repo:
            continue
        issues = _fetch_all_issues(repo)
        log.info("Fetched %d issues from %s", len(issues), repo)
        for issue in issues:
            process_issue(issue, repo)


issues_app = coco.App("praxis-issues", issues_main, repos=",".join(ISSUES_REPOS))


# ---------------------------------------------------------------------------
# App 2b: praxis-discussions — GitHub Discussions → praxis-issues bank
#
# Not covered by DCM/koku's reference pattern. Confirmed real design debate
# lives here that issues alone don't capture (e.g. discussion #838, referenced
# from ai#74's comments, debating shared "model rewrite machinery" for
# semantic/mixture-of-models routing filters). Unlike issue comments, discussion
# comments are NOT filtered to TRUSTED_ASSOCIATIONS: this org is open-source and
# a live investigation found a substantively valuable NONE-association (external
# contributor) comment on ai#74 -- filtering it out the way issue-comment noise
# is filtered would silently drop exactly the kind of technical insight
# Discussions exist to attract.
# ---------------------------------------------------------------------------

_DISCUSSIONS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    discussions(first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        title
        body
        url
        category { name }
        author { login }
        createdAt
        updatedAt
        comments(first: 20) {
          nodes { author { login } body authorAssociation }
        }
      }
    }
  }
}
"""


def _fetch_all_discussions(repo: str) -> list[dict]:
    if "/" not in repo:
        return []
    owner, name = repo.split("/", 1)
    data = _gh_graphql(_DISCUSSIONS_QUERY, owner=owner, name=name)
    nodes = (
        (data.get("data") or {}).get("repository") or {}
    ).get("discussions", {}).get("nodes", []) if data else []
    for n in nodes:
        n["_kind"] = "discussion"
    return nodes


def _format_discussion_header(disc: dict, repo: str) -> str:
    number = disc.get("number", "?")
    title = disc.get("title", "")
    category = (disc.get("category") or {}).get("name", "")
    author = (disc.get("author") or {}).get("login", "unknown")
    created = (disc.get("createdAt") or "")[:10]
    short_repo = _repo_short_name(repo)

    parts = [
        f"# Discussion #{number} ({short_repo}): {title}",
        f"Repo: {repo} | Category: {category} | Author: {author} | Created: {created}",
        "",
    ]
    body = disc.get("body", "") or ""
    if body.strip():
        parts.append(body.strip())
    return "\n".join(parts)


def _filter_discussion_comments(disc: dict) -> list[dict]:
    comments = (disc.get("comments") or {}).get("nodes", []) or []
    return [
        c for c in comments
        if not c.get("author", {}).get("login", "").endswith("[bot]")
        and len(c.get("body", "")) > 20
    ][:10]


@coco.fn(memo=True)
def process_discussion(disc: dict, repo: str) -> None:
    header = _format_discussion_header(disc, repo)
    if not header.strip() or len(header) < 50:
        return

    number = disc.get("number", 0)
    updated_at = disc.get("updatedAt", "")
    category = (disc.get("category") or {}).get("name", "general")
    short_repo = _repo_short_name(repo)

    comment_texts = [_format_comment(c) for c in _filter_discussion_comments(disc)]
    base_doc_id = f"{short_repo}-discussion-{number}"
    sections = chunking.split_issue_sections(header, comment_texts, chunk_size=1200, chunk_overlap=300)
    for key, chunk in sections:
        doc_id = base_doc_id if not key else f"{base_doc_id}-{key}"
        hindsight_retain(
            bank_id="praxis-issues",
            content=chunk,
            document_id=doc_id,
            timestamp=updated_at,
            metadata={
                "source": "cocoindex",
                "repo": repo,
                "kind": "discussion",
                "number": str(number),
                "category": category,
            },
            tags=["discussion", short_repo, category.lower().replace(" ", "-")],
        )


@coco.fn
def discussions_main(repos: str) -> None:
    for repo in repos.split(","):
        repo = repo.strip()
        if not repo:
            continue
        discussions = _fetch_all_discussions(repo)
        log.info("Fetched %d discussions from %s", len(discussions), repo)
        for disc in discussions:
            process_discussion(disc, repo)


discussions_app = coco.App("praxis-discussions", discussions_main, repos=",".join(ISSUES_REPOS))


# ---------------------------------------------------------------------------
# App 2c: praxis-roadmap — org Project (v2) board Status → praxis-issues bank
#
# This is the actual prioritization signal for this org: a Status
# single-select field (Epics -> Backlog -> Next -> In Progress -> Review ->
# Done) applied per-item on each board. One retained document per board
# (grouped by Status) rather than per-item, so a single recall surfaces the
# whole board's current shape instead of requiring N separate lookups.
# ---------------------------------------------------------------------------

_ORG_PROJECTS_QUERY = """
query($org: String!) {
  organization(login: $org) {
    projectsV2(first: 20) {
      nodes { number title shortDescription }
    }
  }
}
"""

_PROJECT_ITEMS_QUERY = """
query($org: String!, $number: Int!, $after: String) {
  organization(login: $org) {
    projectV2(number: $number) {
      title
      shortDescription
      items(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content {
            ... on Issue { number title url state repository { name } }
            ... on PullRequest { number title url state repository { name } }
          }
          fieldValues(first: 10) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _fetch_org_projects(org: str) -> list[dict]:
    data = _gh_graphql(_ORG_PROJECTS_QUERY, org=org)
    nodes = ((data.get("data") or {}).get("organization") or {}).get("projectsV2", {}).get("nodes", []) if data else []
    if nodes:
        return nodes
    log.warning("Org project discovery returned nothing, falling back to known board numbers")
    return [{"number": n, "title": "", "shortDescription": ""} for n in _KNOWN_PROJECT_NUMBERS]


def _item_status(item: dict) -> str:
    for fv in (item.get("fieldValues") or {}).get("nodes", []):
        if (fv.get("field") or {}).get("name") == "Status":
            return fv.get("name", "")
    return "(no status)"


def _fetch_project_items(org: str, number: int) -> tuple[str, str, list[dict]]:
    """Returns (board_title, short_description, items) for one board,
    paginating up to 1000 items (10 pages of 100)."""
    items: list[dict] = []
    board_title = ""
    short_desc = ""
    after: str | None = None
    for _page in range(10):
        variables: dict[str, Any] = {"org": org, "number": number}
        if after:
            variables["after"] = after
        data = _gh_graphql(_PROJECT_ITEMS_QUERY, **variables)
        proj = ((data.get("data") or {}).get("organization") or {}).get("projectV2") if data else None
        if not proj:
            break
        board_title = proj.get("title", board_title)
        short_desc = proj.get("shortDescription", short_desc) or short_desc
        block = proj.get("items", {})
        items.extend(block.get("nodes", []))
        page_info = block.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return board_title, short_desc, items


def _format_board_snapshot(number: int, title: str, short_desc: str, items: list[dict]) -> str:
    by_status: dict[str, list[str]] = {}
    for item in items:
        content = item.get("content") or {}
        if not content:
            continue
        repo_name = (content.get("repository") or {}).get("name", "")
        item_title = content.get("title", "")
        item_number = content.get("number", "?")
        status = _item_status(item)
        by_status.setdefault(status, []).append(f"- #{item_number} ({repo_name}): {item_title}")

    status_order = ["In Progress", "Next", "Review", "Backlog", "Epics", "Done", "(no status)"]
    ordered_statuses = [s for s in status_order if s in by_status] + [
        s for s in by_status if s not in status_order
    ]

    parts = [f"# Project Board: {title} (#{number})"]
    if short_desc:
        parts.append(short_desc)
    parts.append(f"Total items: {len(items)}")
    parts.append("")
    for status in ordered_statuses:
        entries = by_status[status]
        parts.append(f"## Status: {status} ({len(entries)})")
        parts.extend(entries)
        parts.append("")
    return "\n".join(parts)


@coco.fn(memo=True)
def process_board(number: int, org: str) -> None:
    title, short_desc, items = _fetch_project_items(org, number)
    if not title:
        return
    content = _format_board_snapshot(number, title, short_desc, items)
    slug = title.lower().replace(" ", "-").replace("/", "-")
    hindsight_retain(
        bank_id="praxis-issues",
        content=content,
        document_id=f"board-{number}-{slug}",
        timestamp=datetime.utcnow().isoformat(),
        metadata={"source": "cocoindex", "kind": "board", "board_number": str(number), "board_title": title},
        tags=["board", "roadmap", slug],
    )


@coco.fn
def roadmap_main(org: str) -> None:
    boards = _fetch_org_projects(org)
    log.info("Discovered %d org project boards", len(boards))
    for board in boards:
        process_board(board["number"], org)


roadmap_app = coco.App("praxis-roadmap", roadmap_main, org=PRAXIS_ORG)


# ---------------------------------------------------------------------------
# App 3: praxis-code — Rust source → pgvector praxis_code_embeddings table
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CodeEmbedding:
    id: str
    filepath: str
    chunk_index: int
    code: str
    embedding: list[float]
    search_text: str


@coco.fn(memo=True)
async def process_code_file(
    file: localfs.File,
    table: "Any",
    base_dir: pathlib.Path,
    repo_tag: str,
) -> None:
    content = await file.read_text()
    if not content or not content.strip():
        return

    abs_path = str(file.file_path.resolve())
    base_prefix = str(base_dir) + "/"
    rel_path = abs_path.replace(base_prefix, "") if abs_path.startswith(base_prefix) else str(file.file_path.path)
    filepath = f"{repo_tag}/{rel_path}"

    chunks = chunking.split_code(content, filename=filepath, chunk_size=1000, chunk_overlap=300)

    # See docs/FINDINGS.md 2026-08-07: concurrent embed() calls let
    # cocoindex's batching embedder coalesce a file's chunks into one
    # model.encode() call instead of one sequential call per chunk.
    embeddings = await chunking.embed_code_chunks(chunks)
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        row = CodeEmbedding(
            id=f"{filepath}:{i}",
            filepath=filepath,
            chunk_index=i,
            code=chunk,
            embedding=embedding,
            search_text=f"{filepath} {chunk}",
        )
        table.declare_row(row=row)


@coco.fn
async def code_main(org_dir: pathlib.Path) -> None:
    from cocoindex.connectors import postgres

    embedding_dim = await chunking.code_embedding_dim()
    schema = await postgres.TableSchema.from_class(
        CodeEmbedding, primary_key=["id"],
        column_overrides={
            "embedding": postgres.PgType(
                f"vector({embedding_dim})",
                encoder=lambda v: "[" + ",".join(str(x) for x in v) + "]",
            ),
        },
    )
    table = await postgres.mount_table_target(
        PG_POOL, "praxis_code_embeddings", schema, pg_schema_name="cocoindex",
    )
    table.declare_vector_index(column="embedding", metric="cosine")

    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql="""
            ALTER TABLE cocoindex.praxis_code_embeddings
                ADD COLUMN IF NOT EXISTS search_vector tsvector;

            CREATE INDEX IF NOT EXISTS idx_praxis_code_embeddings_fts
                ON cocoindex.praxis_code_embeddings USING gin(search_vector);

            CREATE OR REPLACE FUNCTION cocoindex.update_praxis_code_search_vector()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_praxis_code_search_vector
                ON cocoindex.praxis_code_embeddings;
            CREATE TRIGGER trg_praxis_code_search_vector
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.praxis_code_embeddings
                FOR EACH ROW
                EXECUTE FUNCTION cocoindex.update_praxis_code_search_vector();

            UPDATE cocoindex.praxis_code_embeddings
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, code, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql="""
            DROP TRIGGER IF EXISTS trg_praxis_code_search_vector
                ON cocoindex.praxis_code_embeddings;
            DROP FUNCTION IF EXISTS cocoindex.update_praxis_code_search_vector();
            DROP INDEX IF EXISTS cocoindex.idx_praxis_code_embeddings_fts;
            ALTER TABLE cocoindex.praxis_code_embeddings
                DROP COLUMN IF EXISTS search_vector;
        """,
    )

    for local_dir, _upstream, has_rust in PRAXIS_REPOS:
        if not has_rust:
            continue
        repo_dir = org_dir / local_dir
        files = localfs.walk_dir(
            repo_dir,
            recursive=True,
            path_matcher=PatternFilePathMatcher(
                included_patterns=["**/*.rs"],
                excluded_patterns=["**/target/**"],
            ),
            live=True,
        )
        await coco.mount_each(
            coco.component_subpath(local_dir),
            process_code_file, files.items(),
            table, repo_dir, local_dir,
        )


code_app = coco.App("praxis-code", code_main, org_dir=PRAXIS_ORG_DIR)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _run_live(selected: set[str]) -> None:
    import threading

    threads: list[threading.Thread] = []

    for name, app in [("docs", docs_app), ("code", code_app)]:
        if name not in selected:
            continue
        def _run_app(n=name, a=app):
            log.info("Starting %s app (live, file-watching)...", n)
            try:
                a.update_blocking(live=True)
            except Exception as e:
                log.error("%s app crashed: %s", n, e)
        t = threading.Thread(target=_run_app, name=f"praxis-{name}", daemon=True)
        t.start()
        threads.append(t)

    for name, app, interval in [
        ("issues", issues_app, ISSUES_POLL_INTERVAL),
        ("discussions", discussions_app, ISSUES_POLL_INTERVAL),
        ("roadmap", roadmap_app, ISSUES_POLL_INTERVAL),
    ]:
        if name not in selected:
            continue
        def _poll_loop(n=name, a=app, i=interval):
            while True:
                try:
                    log.info("%s poll: syncing from GitHub...", n)
                    a.update_blocking()
                    log.info("%s poll: complete, next in %ds", n, i)
                except Exception as e:
                    log.error("%s poll error: %s", n, e)
                time.sleep(i)

        log.info("Starting %s app (polling every %ds)...", name, interval)
        t = threading.Thread(target=_poll_loop, name=f"praxis-{name}", daemon=True)
        t.start()
        threads.append(t)

    log.info("All %d apps launched", len(threads))

    for t in threads:
        t.join()


def main():
    parser = argparse.ArgumentParser(
        description="CocoIndex flows for Praxis — incremental ingestion"
    )
    parser.add_argument(
        "--mode",
        choices=["backfill", "live"],
        default="live",
        help="backfill: one-time catch-up; live: continuous watch (default)",
    )
    parser.add_argument(
        "--apps",
        nargs="*",
        choices=["docs", "issues", "discussions", "roadmap", "code"],
        default=None,
        help="Run only specific apps (default: all)",
    )
    args = parser.parse_args()

    selected = set(args.apps) if args.apps else {"docs", "issues", "discussions", "roadmap", "code"}

    log.info("Starting Praxis CocoIndex in %s mode — apps: %s", args.mode, ", ".join(sorted(selected)))
    log.info("  Org dir:          %s", PRAXIS_ORG_DIR)
    log.info("  Manual docs dir:  %s", PRAXIS_MANUAL_DOCS_DIR)
    log.info("  Issues repos:     %s", ", ".join(ISSUES_REPOS))
    log.info("  Hindsight URL:    %s", HINDSIGHT_URL)
    log.info("  CocoIndex DB:     %s", COCOINDEX_DB)

    _wait_for_hindsight()

    if args.mode == "backfill":
        for name, app in [
            ("docs", docs_app), ("issues", issues_app),
            ("discussions", discussions_app), ("roadmap", roadmap_app),
            ("code", code_app),
        ]:
            if name in selected:
                log.info("Running %s app backfill...", name)
                app.update_blocking(report_to_stdout=True)
                log.info("%s app backfill complete", name)
        log.info("All backfills finished")
    else:
        _run_live(selected)


if __name__ == "__main__":
    main()
