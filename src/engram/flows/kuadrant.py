#!/usr/bin/env python3
"""CocoIndex flows for Kuadrant (the Kuadrant GitHub org) — incremental
ingestion into Hindsight and pgvector.

Kuadrant is ingested as read-only prior-art reference material for
praxis-proxy, NOT as a project you develop against here: nobody opens these
checkouts as a Cursor workspace, so there is deliberately no Serena wiring
and no per-repo `.cursor/mcp.json`/git-hooks setup for any of the 8 repos
below (contrast with src/engram/flows/praxis.py, whose repos are all active
dev checkouts). See docs/findings/2026-08.md's 2026-08-27 "Kuadrant
ingestion-only onboarding" entry for the full scoping rationale, including
why the resulting content is surfaced via a single shared, recall-only
`kuadrant` gateway mount cross-wired into every praxis-* repo's
`.cursor/mcp.json`, instead of one gateway entry per Kuadrant repo.

Declares three apps:
  1. kuadrant-docs:   Markdown docs/RFCs from all 8 Kuadrant repos ->
                       Hindsight kuadrant-docs bank
  2. kuadrant-issues: GitHub issues/PRs from all 8 Kuadrant repos ->
                       Hindsight kuadrant-issues bank
  3. kuadrant-code:   Go + Rust source (7 of 8 repos -- `architecture` has
                       no source) -> pgvector kuadrant_code_embeddings table

Plus a non-CocoIndex background thread (`_git_sync_loop`) that periodically
`git pull --ff-only`s all 8 checkouts. Unlike praxis's own repos (pulled
naturally as part of the user's daily dev workflow), nobody routinely
touches these clones -- `localfs.walk_dir(..., live=True)` only watches the
local filesystem for changes, it never fetches upstream itself, so without
this loop the ingested content would silently freeze at initial-clone state
forever instead of actually being "kept up to date" as requested.

Runs as a single long-lived process via launchd. Supports backfill and live
modes.
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
log = logging.getLogger("kuadrant-cocoindex-flows")

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")

KUADRANT_ORG_DIR = pathlib.Path(os.environ.get(
    "KUADRANT_ORG_DIR",
    os.path.expanduser("~/go/src/github.com/Kuadrant"),
))

# Single source of truth for docs/code/issues ingestion: (local checkout dir
# name, upstream "org/repo" name, language). language is "go"/"rust" for
# code_main's per-repo file-pattern selection, or None for `architecture`
# (RFC/design-doc repo with no source code at all -- contributes to docs and
# issues only).
KUADRANT_REPOS: list[tuple[str, str, str | None]] = [
    ("kuadrant-operator", "Kuadrant/kuadrant-operator", "go"),
    ("limitador", "Kuadrant/limitador", "rust"),
    ("wasm-shim", "Kuadrant/wasm-shim", "rust"),
    ("architecture", "Kuadrant/architecture", None),
    ("authorino", "Kuadrant/authorino", "go"),
    ("dns-operator", "Kuadrant/dns-operator", "go"),
    ("authorino-operator", "Kuadrant/authorino-operator", "go"),
    ("limitador-operator", "Kuadrant/limitador-operator", "go"),
]

ISSUES_REPOS = os.environ.get(
    "KUADRANT_ISSUES_REPOS",
    ",".join(upstream for _, upstream, _ in KUADRANT_REPOS),
).split(",")
ISSUES_POLL_INTERVAL = int(os.environ.get("KUADRANT_ISSUES_POLL_SECONDS", "300"))

# How often to `git pull --ff-only` every checkout under KUADRANT_ORG_DIR --
# see module docstring. 6h default: frequent enough that "kept up to date"
# is true in practice, infrequent enough not to hammer GitHub across 8 repos
# on every gateway restart.
GIT_SYNC_INTERVAL_SECONDS = int(os.environ.get("KUADRANT_GIT_SYNC_SECONDS", str(6 * 3600)))

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
    os.path.expanduser("~/.hindsight/kuadrant-cocoindex.db"),
))

# Unique per-file ContextKey name -- see engram-cocoindex-flows.py's PG_POOL
# comment / dcm-cocoindex-flows.py's PG_POOL comment for the full rationale.
PG_POOL: coco.ContextKey[Any] = coco.ContextKey("kuadrant_repo_pg_pool")

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
        # TimeoutError/ConnectionError: see praxis-cocoindex-flows.py's
        # identical comment (docs/FINDINGS.md 2026-08-10) -- a slow
        # hindsight-api under retain-consolidation load raises a raw socket
        # TimeoutError that urllib's (HTTPError, URLError) alone won't catch.
        except (HTTPError, URLError, TimeoutError, ConnectionError) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            log.error("hindsight_retain failed for %s/%s: %s", bank_id, document_id, e)
            return {}


def _repo_short_name(repo: str) -> str:
    return repo.split("/")[-1] if "/" in repo else repo


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
# App 1: kuadrant-docs — Markdown docs/RFCs → Hindsight kuadrant-docs bank
# ---------------------------------------------------------------------------

# rfcs/**/*.md added specifically for `architecture` (its real content --
# numbered RFC files like 0001-rlp-v2.md -- lives under rfcs/, not docs/;
# confirmed via `gh api repos/Kuadrant/architecture/contents/rfcs`). No-op
# for the other 7 repos, none of which has an rfcs/ directory.
_DOC_INCLUDE_PATTERNS = [
    "docs/**/*.md",
    "rfcs/**/*.md",
    "README.md",
    "MAINTAINERS.md",
    "GOVERNANCE.md",
    "CONTRIBUTING.md",
]


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
            bank_id="kuadrant-docs",
            content=chunk,
            document_id=doc_id,
            timestamp=timestamp,
            metadata={"source": "cocoindex", "repo": source_tag},
            tags=[section, source_tag],
        )


@coco.fn
async def docs_main(org_dir: pathlib.Path) -> None:
    for local_dir, _upstream, _language in KUADRANT_REPOS:
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


docs_app = coco.App("kuadrant-docs", docs_main, org_dir=KUADRANT_ORG_DIR)


# ---------------------------------------------------------------------------
# App 2: kuadrant-issues — Issues/PRs → Hindsight kuadrant-issues bank
# ---------------------------------------------------------------------------

def _fetch_all_issues(repo: str) -> list[dict]:
    fields = "number,title,body,state,labels,createdAt,updatedAt,comments,author"
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
    short_repo = _repo_short_name(repo)

    comment_texts = [_format_comment(c) for c in _filter_human_comments(issue)]
    base_doc_id = f"{short_repo}-{kind}-{number}"
    sections = chunking.split_issue_sections(header, comment_texts, chunk_size=1200, chunk_overlap=300)
    for key, chunk in sections:
        doc_id = base_doc_id if not key else f"{base_doc_id}-{key}"
        hindsight_retain(
            bank_id="kuadrant-issues",
            content=chunk,
            document_id=doc_id,
            timestamp=updated_at,
            metadata={
                "source": "cocoindex",
                "repo": repo,
                "kind": kind,
                "number": str(number),
                "state": state,
            },
            tags=[state, kind, short_repo] + labels[:5],
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


issues_app = coco.App("kuadrant-issues", issues_main, repos=",".join(ISSUES_REPOS))


# ---------------------------------------------------------------------------
# App 3: kuadrant-code — Go + Rust source → pgvector kuadrant_code_embeddings
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


_CODE_PATTERNS: dict[str, tuple[list[str], list[str]]] = {
    "go": (["**/*.go"], ["**/vendor/**", "**/*_test.go", "**/zz_generated*"]),
    "rust": (["**/*.rs"], ["**/target/**"]),
}


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
        PG_POOL, "kuadrant_code_embeddings", schema, pg_schema_name="cocoindex",
    )
    table.declare_vector_index(column="embedding", metric="cosine")

    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql="""
            ALTER TABLE cocoindex.kuadrant_code_embeddings
                ADD COLUMN IF NOT EXISTS search_vector tsvector;

            CREATE INDEX IF NOT EXISTS idx_kuadrant_code_embeddings_fts
                ON cocoindex.kuadrant_code_embeddings USING gin(search_vector);

            CREATE OR REPLACE FUNCTION cocoindex.update_kuadrant_code_search_vector()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_kuadrant_code_search_vector
                ON cocoindex.kuadrant_code_embeddings;
            CREATE TRIGGER trg_kuadrant_code_search_vector
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.kuadrant_code_embeddings
                FOR EACH ROW
                EXECUTE FUNCTION cocoindex.update_kuadrant_code_search_vector();

            UPDATE cocoindex.kuadrant_code_embeddings
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, code, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql="""
            DROP TRIGGER IF EXISTS trg_kuadrant_code_search_vector
                ON cocoindex.kuadrant_code_embeddings;
            DROP FUNCTION IF EXISTS cocoindex.update_kuadrant_code_search_vector();
            DROP INDEX IF EXISTS cocoindex.idx_kuadrant_code_embeddings_fts;
            ALTER TABLE cocoindex.kuadrant_code_embeddings
                DROP COLUMN IF EXISTS search_vector;
        """,
    )

    for local_dir, _upstream, language in KUADRANT_REPOS:
        if language not in _CODE_PATTERNS:
            continue
        included, excluded = _CODE_PATTERNS[language]
        repo_dir = org_dir / local_dir
        files = localfs.walk_dir(
            repo_dir,
            recursive=True,
            path_matcher=PatternFilePathMatcher(included_patterns=included, excluded_patterns=excluded),
            live=True,
        )
        await coco.mount_each(
            coco.component_subpath(local_dir),
            process_code_file, files.items(),
            table, repo_dir, local_dir,
        )


code_app = coco.App("kuadrant-code", code_main, org_dir=KUADRANT_ORG_DIR)


# ---------------------------------------------------------------------------
# Git sync — periodic `git pull` across all 8 checkouts (not a CocoIndex
# app: no source/sink, just keeps the on-disk clones that docs_main/
# code_main watch from going permanently stale). See module docstring.
# ---------------------------------------------------------------------------

def _git_pull_one(repo_dir: pathlib.Path) -> None:
    if not (repo_dir / ".git").exists():
        return
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only", "--quiet"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            log.warning("git pull failed for %s: %s", repo_dir, result.stderr[:300])
        else:
            log.info("git pull OK for %s", repo_dir.name)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("git pull error for %s: %s", repo_dir, e)


def _git_sync_loop(org_dir: pathlib.Path, interval: int) -> None:
    while True:
        for local_dir, _upstream, _language in KUADRANT_REPOS:
            _git_pull_one(org_dir / local_dir)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _run_live(selected: set[str]) -> None:
    import threading

    threads: list[threading.Thread] = []

    if "git-sync" in selected:
        t = threading.Thread(
            target=_git_sync_loop, name="kuadrant-git-sync",
            args=(KUADRANT_ORG_DIR, GIT_SYNC_INTERVAL_SECONDS), daemon=True,
        )
        t.start()
        threads.append(t)
        log.info("Started git-sync thread (every %ds)", GIT_SYNC_INTERVAL_SECONDS)

    for name, app in [("docs", docs_app), ("code", code_app)]:
        if name not in selected:
            continue
        def _run_app(n=name, a=app):
            log.info("Starting %s app (live, file-watching)...", n)
            try:
                a.update_blocking(live=True)
            except Exception as e:
                log.error("%s app crashed: %s", n, e)
        t = threading.Thread(target=_run_app, name=f"kuadrant-{name}", daemon=True)
        t.start()
        threads.append(t)

    if "issues" in selected:
        def _issues_poll_loop():
            while True:
                try:
                    log.info("issues poll: syncing from GitHub...")
                    issues_app.update_blocking()
                    log.info("issues poll: complete, next in %ds", ISSUES_POLL_INTERVAL)
                except Exception as e:
                    log.error("issues poll error: %s", e)
                time.sleep(ISSUES_POLL_INTERVAL)

        log.info("Starting issues app (polling every %ds)...", ISSUES_POLL_INTERVAL)
        t = threading.Thread(target=_issues_poll_loop, name="kuadrant-issues", daemon=True)
        t.start()
        threads.append(t)

    log.info("All %d threads launched", len(threads))

    for t in threads:
        t.join()


def main():
    parser = argparse.ArgumentParser(
        description="CocoIndex flows for Kuadrant — incremental ingestion"
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
        choices=["docs", "issues", "code", "git-sync"],
        default=None,
        help="Run only specific apps (default: all)",
    )
    args = parser.parse_args()

    selected = set(args.apps) if args.apps else {"docs", "issues", "code", "git-sync"}

    log.info("Starting Kuadrant CocoIndex in %s mode — apps: %s", args.mode, ", ".join(sorted(selected)))
    log.info("  Org dir:          %s", KUADRANT_ORG_DIR)
    log.info("  Issues repos:     %s", ", ".join(ISSUES_REPOS))
    log.info("  Hindsight URL:    %s", HINDSIGHT_URL)
    log.info("  CocoIndex DB:     %s", COCOINDEX_DB)

    _wait_for_hindsight()

    if args.mode == "backfill":
        if "git-sync" in selected:
            for local_dir, _upstream, _language in KUADRANT_REPOS:
                _git_pull_one(KUADRANT_ORG_DIR / local_dir)
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
