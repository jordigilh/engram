#!/usr/bin/env python3
"""CocoIndex flows for rhdh-plugins — incremental ingestion into Hindsight and pgvector.

Declares three apps:
  1. rhdh-plugins-docs:   Markdown specs from workspaces/boost/ + repo-root READMEs
                          → Hindsight rhdh-plugins-docs bank
  2. rhdh-plugins-issues: Jira (project RHIDP), scoped to a single epic and its
                          children → Hindsight rhdh-plugins-issues bank
  3. rhdh-plugins-code:   TypeScript source under workspaces/boost/
                          → pgvector rhdh_plugins_code_embeddings table

Deliberately narrower than every other onboarded project: rhdh-plugins is a
23-package Yarn monorepo, but the actual work here is scoped to one Jira
epic (RHIDP-15270, "AI Catalog Graduated Visibility Permissions") and the
one workspace package that epic's stories touch (workspaces/boost/ -- the
only package referencing ai-catalog/SkillBundle, confirmed by grep during
onboarding). Docs/code ingestion is scoped to that same package rather than
the whole monorepo; Jira ingestion is scoped to that one epic's subtree
rather than the whole RHIDP project (166+ open issues) the way koku ingests
its whole Jira project (COST) -- see docs/NEW_PROJECT_SETUP.md's "epic-scoped
Jira variant". If the epic scope ever needs to widen, change
RHDH_PLUGINS_JIRA_EPIC (or generalize the JQL) rather than dropping the
scoping entirely.

No GitHub-issues/PRs ingestion here at all, unlike every prior project: this
repo's real project management for this work lives entirely in Jira, not
GitHub Issues (166 open GitHub issues exist but are out of scope for this
narrow epic-driven onboarding).

Runs as a single long-lived process via launchd. Supports backfill and live modes.
"""

import argparse
import base64
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
log = logging.getLogger("rhdh-plugins-cocoindex-flows")

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")

RHDH_PLUGINS_REPO_DIR = pathlib.Path(os.environ.get(
    "RHDH_PLUGINS_REPO_DIR",
    os.path.expanduser("~/go/src/github.com/redhat-developer/rhdh-plugins"),
))
# The one workspace package this onboarding's scope actually covers -- see
# module docstring. Not the whole monorepo.
RHDH_PLUGINS_BOOST_DIR = pathlib.Path(os.environ.get(
    "RHDH_PLUGINS_BOOST_DIR",
    str(RHDH_PLUGINS_REPO_DIR / "workspaces" / "boost"),
))

# rhdh-plugins' real project management for this scope is Jira (project
# RHIDP), not GitHub Issues -- see module docstring. Scoped to one epic and
# its children, not the whole RHIDP project (166+ open issues, most
# unrelated to this work).
JIRA_EPIC = os.environ.get("RHDH_PLUGINS_JIRA_EPIC", "RHIDP-15270")
JIRA_SERVER = os.environ.get("RHDH_PLUGINS_JIRA_SERVER", "https://redhat.atlassian.net")
JIRA_LOGIN_EMAIL = os.environ.get("RHDH_PLUGINS_JIRA_EMAIL", "jgil@redhat.com")
ISSUES_POLL_INTERVAL = int(os.environ.get("RHDH_PLUGINS_ISSUES_POLL_SECONDS", "300"))
JIRA_FIELDS = [
    "summary", "description", "status", "issuetype", "priority",
    "labels", "reporter", "created", "updated", "comment", "parent",
]

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
    os.path.expanduser("~/.hindsight/rhdh-plugins-cocoindex.db"),
))

# Unique per-file ContextKey name -- NEW_PROJECT_SETUP.md's "Gotcha" (see
# engram-cocoindex-flows.py's PG_POOL for the reference example). Do NOT use
# the generic "pg_pool" name; CocoIndex ContextKeys are process-global.
PG_POOL: coco.ContextKey[Any] = coco.ContextKey("rhdh_plugins_repo_pg_pool")


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
        except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            log.error("hindsight_retain failed for %s/%s: %s", bank_id, document_id, e)
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
# App 1: rhdh-plugins-docs — Markdown specs → Hindsight rhdh-plugins-docs bank
# ---------------------------------------------------------------------------

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
    sections = chunking.split_markdown_sections(content, chunk_size=800, chunk_overlap=200)
    for key, chunk in sections:
        doc_id = base_doc_id if not key else f"{base_doc_id}--{key}"
        hindsight_retain(
            bank_id="rhdh-plugins-docs",
            content=chunk,
            document_id=doc_id,
            timestamp=timestamp,
            metadata={"source": "cocoindex", "repo": source_tag},
            tags=[section, source_tag],
        )


@coco.fn
async def docs_main(repo_dir: pathlib.Path, boost_dir: pathlib.Path) -> None:
    # workspaces/boost/specifications/ -- PRD + Jira-analysis docs for this scope.
    specs_dir = boost_dir / "specifications"
    specs_tree = localfs.walk_dir(
        specs_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*.md"]),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("boost-specifications"),
        process_doc_file, specs_tree.items(),
        specs_dir, "rhdh-plugins-boost-specifications",
    )

    # workspaces/boost/openspec/changes/ -- structured per-feature spec docs
    # (e.g. ai-catalog-asset-governance, ai-catalog-frontend) directly
    # relevant to this epic's scope.
    openspec_dir = boost_dir / "openspec" / "changes"
    openspec_tree = localfs.walk_dir(
        openspec_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*.md"]),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("boost-openspec"),
        process_doc_file, openspec_tree.items(),
        openspec_dir, "rhdh-plugins-boost-openspec",
    )

    # Repo-root READMEs for overall project context.
    readme_files = localfs.walk_dir(
        repo_dir,
        recursive=False,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["README.md", "CONTRIBUTING.md"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("readme"),
        process_doc_file, readme_files.items(),
        repo_dir, "rhdh-plugins-readme",
    )


docs_app = coco.App(
    "rhdh-plugins-docs", docs_main,
    repo_dir=RHDH_PLUGINS_REPO_DIR,
    boost_dir=RHDH_PLUGINS_BOOST_DIR,
)


# ---------------------------------------------------------------------------
# App 2: rhdh-plugins-issues — Jira epic RHIDP-15270 + children → Hindsight
# rhdh-plugins-issues bank
# ---------------------------------------------------------------------------
#
# Reuses koku-cocoindex-flows.py's Jira integration verbatim (ADF flattening,
# Keychain token lookup, /rest/api/3/search/jql pagination) -- see that
# file's _jira_token()/_fetch_all_jira_issues() docstrings for the full
# rationale on why this hits the real Cloud search endpoint directly instead
# of shelling out to the `jira` CLI. The only real difference here is JQL
# scope: `parent = <epic> OR key = <epic>` (one epic + its children) instead
# of `project = <PROJECT>` (koku's whole Jira project).

def _adf_to_text(node: Any) -> str:
    """Flatten a Jira Atlassian Document Format (ADF) node tree into plain
    text. Good enough for search/retain purposes -- not a full renderer."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "mention":
        return node.get("attrs", {}).get("text", "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "rule":
        return "\n---\n"

    inner = "".join(_adf_to_text(c) for c in node.get("content", []))
    if node_type in ("paragraph", "heading", "listItem", "codeBlock"):
        return inner + "\n"
    return inner


def _jira_token() -> str | None:
    """Read the `jira` CLI's (ankitpokhrel/jira-cli) Keychain-stored token
    directly rather than shelling out to the CLI itself -- see
    koku-cocoindex-flows.py's _jira_token() for the full rationale (jira-cli's
    --paginate has a real bug against Jira Cloud's newer /search/jql
    endpoint; calling that endpoint's real nextPageToken contract directly
    avoids it and needs no subprocess wrapper around the CLI at all)."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "jira-cli", "-s", "jira-cloud-api-token", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            log.error("Could not read jira-cli API token from macOS Keychain")
            return None
        return token
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("_jira_token: keychain lookup failed: %s", e)
        return None


def _fetch_epic_and_children(epic_key: str, page_size: int = 100) -> list[dict]:
    """Paginate `POST /rest/api/3/search/jql` for the epic itself plus every
    direct child story/task under it. Each page already includes
    description + comments (as ADF) -- no N+1 per-issue fetch needed.

    Deliberately no upper LIMIT (unlike koku's KOKU_JIRA_LIMIT): this scope
    is one epic's subtree, expected to be a handful of issues, not koku's
    5+-year/7,800-issue whole-project history that LIMIT exists to cap."""
    token = _jira_token()
    if token is None:
        return []
    auth = base64.b64encode(f"{JIRA_LOGIN_EMAIL}:{token}".encode()).decode()

    all_items: list[dict] = []
    next_page_token: str | None = None
    jql = f'parent = {epic_key} OR key = {epic_key} order by created asc'
    while True:
        body: dict[str, Any] = {
            "jql": jql,
            "maxResults": page_size,
            "fields": JIRA_FIELDS,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        req = Request(
            f"{JIRA_SERVER}/rest/api/3/search/jql",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            log.error("_fetch_epic_and_children request failed: %s", e)
            break

        batch = data.get("issues", [])
        all_items.extend(batch)
        log.info("Fetched %d Jira issues for epic %s (total so far: %d)", len(batch), epic_key, len(all_items))

        if data.get("isLast", True) or not batch:
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
    return all_items


def _format_jira_issue_header(issue: dict) -> str:
    """Static (non-volatile) header for a Jira issue: key/summary/reporter/
    created plus the description. Deliberately excludes status/priority/
    labels -- those get triaged routinely, and are already carried in the
    retain call's `metadata`/`tags`. See docs/FINDINGS.md 2026-08-03.
    """
    fields = issue.get("fields", {}) or {}
    key = issue.get("key", "?")
    summary = fields.get("summary", "")
    issue_type = (fields.get("issuetype") or {}).get("name", "Issue")
    reporter = (fields.get("reporter") or {}).get("displayName", "unknown")
    created = (fields.get("created") or "")[:10]
    parent = fields.get("parent") or {}
    parent_key = parent.get("key", "")

    parts = [
        f"# {issue_type} {key}: {summary}",
        f"Project: RHIDP | Reporter: {reporter} | Created: {created}"
        + (f" | Epic: {parent_key}" if parent_key else ""),
        "",
    ]
    description = _adf_to_text(fields.get("description")).strip()
    if description:
        parts.append(description)
    return "\n".join(parts)


def _filter_jira_comments(issue: dict) -> list[dict]:
    fields = issue.get("fields", {}) or {}
    comments = ((fields.get("comment") or {}).get("comments", [])) or []
    return [c for c in comments if len(_adf_to_text(c.get("body")).strip()) > 20][:10]


def _format_jira_comment(comment: dict) -> str:
    author = (comment.get("author") or {}).get("displayName", "?")
    body = _adf_to_text(comment.get("body")).strip()
    if len(body) > 2000:
        body = body[:2000] + "\n[...truncated]"
    return f"**{author}:**\n{body}"


@coco.fn(memo=True)
def process_jira_issue(issue: dict) -> None:
    """Format, chunk, and push a single Jira issue (epic or child story) to
    Hindsight. See process_pr's identical chunking rationale in
    koku-cocoindex-flows.py and docs/FINDINGS.md 2026-08-03."""
    header = _format_jira_issue_header(issue)
    if not header.strip() or len(header) < 50:
        return

    fields = issue.get("fields", {}) or {}
    key = issue.get("key", "unknown")
    status = ((fields.get("status") or {}).get("name") or "unknown").lower()
    issue_type = ((fields.get("issuetype") or {}).get("name") or "issue").lower()
    labels = fields.get("labels", []) or []
    updated = fields.get("updated", "")
    parent = fields.get("parent") or {}
    parent_key = parent.get("key", "")

    comment_texts = [_format_jira_comment(c) for c in _filter_jira_comments(issue)]
    base_doc_id = f"rhdh-plugins-jira-{key}"

    sections = chunking.split_issue_sections(header, comment_texts, chunk_size=12000, chunk_overlap=500)
    for key_suffix, chunk in sections:
        doc_id = base_doc_id if not key_suffix else f"{base_doc_id}-{key_suffix}"
        tags = [status, issue_type, "jira", "RHIDP"] + labels[:5]
        if parent_key:
            tags.append(parent_key)
        hindsight_retain(
            bank_id="rhdh-plugins-issues",
            content=chunk,
            document_id=doc_id,
            timestamp=updated,
            metadata={
                "source": "cocoindex",
                "tracker": "jira",
                "project": "RHIDP",
                "key": key,
                "status": status,
                "epic": JIRA_EPIC,
            },
            tags=tags,
        )


@coco.fn
def issues_main(epic_key: str) -> None:
    jira_issues = _fetch_epic_and_children(epic_key)
    for issue in jira_issues:
        process_jira_issue(issue)


issues_app = coco.App("rhdh-plugins-issues", issues_main, epic_key=JIRA_EPIC)


# ---------------------------------------------------------------------------
# App 3: rhdh-plugins-code — TypeScript source (workspaces/boost/ only)
# → pgvector rhdh_plugins_code_embeddings table
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
async def code_main(boost_dir: pathlib.Path) -> None:
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
        PG_POOL, "rhdh_plugins_code_embeddings", schema, pg_schema_name="cocoindex",
    )
    table.declare_vector_index(column="embedding", metric="cosine")

    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql="""
            ALTER TABLE cocoindex.rhdh_plugins_code_embeddings
                ADD COLUMN IF NOT EXISTS search_vector tsvector;

            CREATE INDEX IF NOT EXISTS idx_rhdh_plugins_code_embeddings_fts
                ON cocoindex.rhdh_plugins_code_embeddings USING gin(search_vector);

            CREATE OR REPLACE FUNCTION cocoindex.update_rhdh_plugins_code_search_vector()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_rhdh_plugins_code_search_vector
                ON cocoindex.rhdh_plugins_code_embeddings;
            CREATE TRIGGER trg_rhdh_plugins_code_search_vector
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.rhdh_plugins_code_embeddings
                FOR EACH ROW
                EXECUTE FUNCTION cocoindex.update_rhdh_plugins_code_search_vector();

            UPDATE cocoindex.rhdh_plugins_code_embeddings
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, code, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql="""
            DROP TRIGGER IF EXISTS trg_rhdh_plugins_code_search_vector
                ON cocoindex.rhdh_plugins_code_embeddings;
            DROP FUNCTION IF EXISTS cocoindex.update_rhdh_plugins_code_search_vector();
            DROP INDEX IF EXISTS cocoindex.idx_rhdh_plugins_code_embeddings_fts;
            ALTER TABLE cocoindex.rhdh_plugins_code_embeddings
                DROP COLUMN IF EXISTS search_vector;
        """,
    )

    files = localfs.walk_dir(
        boost_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.ts", "**/*.tsx"],
            excluded_patterns=[
                "**/node_modules/**",
                "**/dist/**",
                "**/dist-dynamic/**",
                "**/*.test.ts",
                "**/*.test.tsx",
                "**/coverage/**",
            ],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("boost"),
        process_code_file, files.items(),
        table, boost_dir, "rhdh-plugins-boost",
    )


code_app = coco.App(
    "rhdh-plugins-code", code_main,
    boost_dir=RHDH_PLUGINS_BOOST_DIR,
)


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
        t = threading.Thread(target=_run_app, name=f"rhdh-plugins-{name}", daemon=True)
        t.start()
        threads.append(t)

    if "issues" in selected:
        def _issues_poll_loop():
            while True:
                try:
                    log.info("Issues poll: syncing from Jira...")
                    issues_app.update_blocking()
                    log.info("Issues poll: complete, next in %ds", ISSUES_POLL_INTERVAL)
                except Exception as e:
                    log.error("Issues poll error: %s", e)
                time.sleep(ISSUES_POLL_INTERVAL)

        log.info("Starting issues app (polling every %ds)...", ISSUES_POLL_INTERVAL)
        t = threading.Thread(target=_issues_poll_loop, name="rhdh-plugins-issues", daemon=True)
        t.start()
        threads.append(t)

    log.info("All %d apps launched — docs/code watching files, issues polling every %ds",
             len(threads), ISSUES_POLL_INTERVAL)

    for t in threads:
        t.join()


def main():
    parser = argparse.ArgumentParser(
        description="CocoIndex flows for rhdh-plugins — incremental ingestion"
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
        choices=["docs", "issues", "code"],
        default=None,
        help="Run only specific apps (default: all)",
    )
    args = parser.parse_args()

    selected = set(args.apps) if args.apps else {"docs", "issues", "code"}

    log.info("Starting rhdh-plugins CocoIndex in %s mode — apps: %s", args.mode, ", ".join(sorted(selected)))
    log.info("  Repo dir:      %s", RHDH_PLUGINS_REPO_DIR)
    log.info("  Boost dir:     %s", RHDH_PLUGINS_BOOST_DIR)
    log.info("  Jira epic:     %s", JIRA_EPIC)
    log.info("  Hindsight URL: %s", HINDSIGHT_URL)
    log.info("  CocoIndex DB:  %s", COCOINDEX_DB)

    _wait_for_hindsight()

    if args.mode == "backfill":
        for name, app in [("docs", docs_app), ("issues", issues_app), ("code", code_app)]:
            if name in selected:
                log.info("Running %s app backfill...", name)
                app.update_blocking(report_to_stdout=True)
                log.info("%s app backfill complete", name)
        log.info("All backfills finished")
    else:
        _run_live(selected)


if __name__ == "__main__":
    main()
