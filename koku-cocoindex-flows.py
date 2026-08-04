#!/usr/bin/env python3
"""CocoIndex flows for Koku — incremental ingestion into Hindsight and pgvector.

Declares three apps:
  1. koku-docs:   Markdown docs from the koku repo    → Hindsight koku-docs bank
  2. koku-issues: GitHub issues from project-koku/koku → Hindsight koku-issues bank
  3. koku-code:   Python source                        → pgvector koku_code_embeddings table

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
import time
from datetime import datetime
from typing import Any, AsyncIterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import cocoindex as coco
from cocoindex.connectors import localfs
from cocoindex.resources.file import PatternFilePathMatcher

import chunking

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("koku-cocoindex-flows")

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")

KOKU_REPO_DIR = pathlib.Path(os.environ.get(
    "KOKU_REPO_DIR",
    os.path.expanduser("~/go/src/github.com/project-koku/koku"),
))
KOKU_DOCS_DIR = pathlib.Path(os.environ.get(
    "KOKU_DOCS_DIR",
    str(KOKU_REPO_DIR / "docs"),
))

# Koku's real issue tracker is Jira (project COST, see
# https://issues.redhat.com/projects/COST/ -- linked from the repo's own
# README), not GitHub Issues. PR_REPOS is GitHub-PR-only (code review still
# happens on GitHub even though ticket tracking doesn't).
PR_REPOS = os.environ.get(
    "KOKU_PR_REPOS",
    "project-koku/koku",
).split(",")
JIRA_PROJECT = os.environ.get("KOKU_JIRA_PROJECT", "COST")
ISSUES_POLL_INTERVAL = int(os.environ.get("KOKU_ISSUES_POLL_SECONDS", "300"))

PG_DSN = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
# See cocoindex-flows.py's PG_POOL_MIN_SIZE/MAX_SIZE comment (docs/FINDINGS.md
# 2026-08-03) -- asyncpg's own min_size=10/max_size=10 default is oversized
# for this pool's light, bursty pgvector-upsert-only workload, and each
# onboarded project's own cocoindex-flows.py multiplies it against the same
# shared Postgres instance.
PG_POOL_MIN_SIZE = int(os.environ.get("COCOINDEX_PG_POOL_MIN_SIZE", "2"))
PG_POOL_MAX_SIZE = int(os.environ.get("COCOINDEX_PG_POOL_MAX_SIZE", "5"))
COCOINDEX_DB = pathlib.Path(os.environ.get(
    "COCOINDEX_DB",
    os.path.expanduser("~/.hindsight/koku-cocoindex.db"),
))

# Unique per-file ContextKey name -- NEW_PROJECT_SETUP.md's "Gotcha" (see
# engram-cocoindex-flows.py's PG_POOL for the reference example). Do NOT use
# the generic "pg_pool" name that the main cocoindex-flows.py already
# registers: CocoIndex ContextKeys are process-global, and a second
# registration of the same name raises ValueError if two flow files ever
# load into one process (e.g. pytest collection).
PG_POOL: coco.ContextKey[Any] = coco.ContextKey("koku_repo_pg_pool")

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
    # See cocoindex-flows.py's hindsight_retain() docstring: strategy="exact"
    # was never registered in any bank's retain_strategies config, so
    # hindsight-api silently ignored it -- pure log noise, no behavior.
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
        except (HTTPError, URLError) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            log.error("hindsight_retain failed for %s/%s: %s", bank_id, document_id, e)
            return {}


def _split_text(text: str, chunk_size: int = 800, chunk_overlap: int = 200) -> list[str]:
    """Thin alias to chunking.split_fixed_window() -- still used directly by
    code_app below. process_doc_file/process_pr/process_jira_issue now use
    chunking.split_markdown_sections()/split_issue_sections() instead. See
    docs/FINDINGS.md 2026-08-03."""
    return chunking.split_fixed_window(text, chunk_size, chunk_overlap)


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
# App 1: koku-docs — Markdown docs → Hindsight koku-docs bank
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
            bank_id="koku-docs",
            content=chunk,
            document_id=doc_id,
            timestamp=timestamp,
            metadata={"source": "cocoindex", "repo": source_tag},
            tags=[section, source_tag],
        )


@coco.fn
async def docs_main(
    repo_dir: pathlib.Path,
    docs_dir: pathlib.Path,
) -> None:
    docs_tree = localfs.walk_dir(
        docs_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.md"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("docs"),
        process_doc_file, docs_tree.items(),
        docs_dir, "koku-docs-tree",
    )

    readme_files = localfs.walk_dir(
        repo_dir,
        recursive=False,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["README.md", "CONTRIBUTING.md", "AGENTS.md"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("readme"),
        process_doc_file, readme_files.items(),
        repo_dir, "koku-readme",
    )


docs_app = coco.App(
    "koku-docs", docs_main,
    repo_dir=KOKU_REPO_DIR,
    docs_dir=KOKU_DOCS_DIR,
)


# ---------------------------------------------------------------------------
# App 2: koku-issues — GitHub PRs + Jira (project COST) → Hindsight koku-issues bank
# ---------------------------------------------------------------------------

def _fetch_all_prs(repo: str) -> list[dict]:
    """GitHub PRs only -- koku's actual issue/ticket tracking lives in Jira
    (project COST), not GitHub Issues, but code review discussion still
    happens on GitHub PRs, so those remain worth ingesting here."""
    fields = "number,title,body,state,labels,createdAt,updatedAt,comments,author"
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "all",
        "--limit", "10000",
        "--json", fields,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.error("gh pr list failed: %s", result.stderr[:300])
            return []
        batch = json.loads(result.stdout)
        for item in batch:
            item["_kind"] = "pr"
        log.info("Fetched %d PRs from %s", len(batch), repo)
        return batch
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        log.error("_fetch_all_prs error: %s", e)
        return []


def _repo_short_name(repo: str) -> str:
    return repo.split("/")[-1] if "/" in repo else repo


def _format_issue_header(issue: dict, repo: str) -> str:
    """Static (non-volatile) header for a PR: title/repo/author/created plus
    the description body. Deliberately excludes state/labels -- those
    change routinely (PRs get merged, labels get triaged) and are already
    carried in `metadata`/`tags`. See docs/FINDINGS.md 2026-08-03.
    """
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
def process_pr(pr: dict, repo: str) -> None:
    """Format, chunk, and push a single PR to Hindsight.

    Chunked as one section for the header+description plus one section per
    comment (chunking.split_issue_sections()), keyed by ordinal comment
    position rather than character offset -- see docs/FINDINGS.md
    2026-08-03.
    """
    header = _format_issue_header(pr, repo)
    if not header.strip() or len(header) < 50:
        return

    number = pr.get("number", 0)
    updated_at = pr.get("updatedAt", "")
    state = pr.get("state", "OPEN").lower()
    kind = pr.get("_kind", "pr")
    labels = [label.get("name", "") for label in pr.get("labels", [])]
    short_repo = _repo_short_name(repo)

    comment_texts = [_format_comment(c) for c in _filter_human_comments(pr)]
    base_doc_id = f"{short_repo}-{kind}-{number}"

    # Deliberately much larger than docs/code chunking: this feeds
    # hindsight_retain()'s LLM extraction, not an embedding index, and each
    # call pays a large fixed system-prompt overhead (~3,550 tokens,
    # measured empirically) regardless of chunk size. Smaller chunks here
    # only multiply that fixed cost for no retrieval-quality benefit --
    # measured ~55-60% cheaper at this size vs. the docs-app-inherited 1200
    # default across koku's ~13k-item issue/PR backlog, with no downside
    # since Haiku 4.5's 200K context makes this trivially safe.
    sections = chunking.split_issue_sections(header, comment_texts, chunk_size=12000, chunk_overlap=500)
    for key, chunk in sections:
        doc_id = base_doc_id if not key else f"{base_doc_id}-{key}"
        hindsight_retain(
            bank_id="koku-issues",
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


# ---------------------------------------------------------------------------
# Jira (project COST) -- koku's actual issue/ticket tracker
# ---------------------------------------------------------------------------

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


JIRA_SERVER = os.environ.get("KOKU_JIRA_SERVER", "https://redhat.atlassian.net")
JIRA_LOGIN_EMAIL = os.environ.get("KOKU_JIRA_EMAIL", "jgil@redhat.com")
JIRA_FIELDS = [
    "summary", "description", "status", "issuetype", "priority",
    "labels", "reporter", "created", "updated", "comment",
]


def _jira_token() -> str | None:
    """The `jira` CLI (ankitpokhrel/jira-cli) is only usable interactively --
    it's a zsh function that injects this same Keychain-sourced token as
    JIRA_API_TOKEN before calling the real binary. Read the Keychain entry
    directly instead of shelling out to `jira` at all: `jira issue list
    --paginate <offset>:<limit>` was found to silently repeat the same page
    forever once an --order-by is specified (or drift/duplicate without one)
    against a ~7,800-issue project -- a jira-cli limitation against Jira
    Cloud's newer token-cursor-only /search/jql endpoint, not something
    fixable via JQL. Calling that endpoint directly with its real
    nextPageToken contract (verified empirically: clean, non-overlapping
    pages) avoids the bug entirely and needs no subprocess at all."""
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


def _fetch_all_jira_issues(project: str, page_size: int = 100) -> list[dict]:
    """Paginate `POST /rest/api/3/search/jql` directly via nextPageToken.
    Each page already includes description + comments (as ADF) -- no N+1
    per-issue fetch needed, unlike jira-cli's `view` command."""
    token = _jira_token()
    if token is None:
        return []
    auth = base64.b64encode(f"{JIRA_LOGIN_EMAIL}:{token}".encode()).decode()

    all_items: list[dict] = []
    next_page_token: str | None = None
    while True:
        body: dict[str, Any] = {
            "jql": f"project = {project} order by key asc",
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
            log.error("_fetch_all_jira_issues request failed: %s", e)
            break

        batch = data.get("issues", [])
        all_items.extend(batch)
        log.info("Fetched %d Jira issues from %s (total so far: %d)", len(batch), project, len(all_items))

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
    issue_type = (fields.get("issueType") or {}).get("name", "Issue")
    reporter = (fields.get("reporter") or {}).get("displayName", "unknown")
    created = (fields.get("created") or "")[:10]

    parts = [
        f"# {issue_type} {key}: {summary}",
        f"Project: COST | Reporter: {reporter} | Created: {created}",
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
    """Format, chunk, and push a single Jira issue to Hindsight.

    Chunked as one section for the header+description plus one section per
    comment, keyed by ordinal comment position -- see process_pr's
    identical rationale and docs/FINDINGS.md 2026-08-03.
    """
    header = _format_jira_issue_header(issue)
    if not header.strip() or len(header) < 50:
        return

    fields = issue.get("fields", {}) or {}
    key = issue.get("key", "unknown")
    status = ((fields.get("status") or {}).get("name") or "unknown").lower()
    issue_type = ((fields.get("issueType") or {}).get("name") or "issue").lower()
    labels = fields.get("labels", []) or []
    updated = fields.get("updated", "")

    comment_texts = [_format_jira_comment(c) for c in _filter_jira_comments(issue)]
    base_doc_id = f"koku-jira-{key}"

    # See process_pr's identical comment: retain()-only (no embedding), so a
    # large chunk size only reduces fixed per-call overhead cost, never hurts.
    sections = chunking.split_issue_sections(header, comment_texts, chunk_size=12000, chunk_overlap=500)
    for key_suffix, chunk in sections:
        doc_id = base_doc_id if not key_suffix else f"{base_doc_id}-{key_suffix}"
        hindsight_retain(
            bank_id="koku-issues",
            content=chunk,
            document_id=doc_id,
            timestamp=updated,
            metadata={
                "source": "cocoindex",
                "tracker": "jira",
                "project": JIRA_PROJECT,
                "key": key,
                "status": status,
            },
            tags=[status, issue_type, "jira", JIRA_PROJECT] + labels[:5],
        )


@coco.fn
def issues_main(repos: str, jira_project: str) -> None:
    for repo in repos.split(","):
        repo = repo.strip()
        if not repo:
            continue
        prs = _fetch_all_prs(repo)
        for pr in prs:
            process_pr(pr, repo)

    jira_issues = _fetch_all_jira_issues(jira_project)
    for issue in jira_issues:
        process_jira_issue(issue)


issues_app = coco.App("koku-issues", issues_main, repos=",".join(PR_REPOS), jira_project=JIRA_PROJECT)


# ---------------------------------------------------------------------------
# App 3: koku-code — Python source → pgvector koku_code_embeddings table
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CodeEmbedding:
    id: str
    filepath: str
    chunk_index: int
    code: str
    embedding: list[float]
    search_text: str

_embedder = None

def _get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        log.info("Loaded embedding model: all-MiniLM-L6-v2")
    return _embedder


def _embed_text(text: str) -> list[float]:
    model = _get_embedder()
    return model.encode(text, normalize_embeddings=True).tolist()


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

    chunks = _split_text(content, chunk_size=1000, chunk_overlap=300)

    for i, chunk in enumerate(chunks):
        embedding = _embed_text(chunk)
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
async def code_main(
    repo_dir: pathlib.Path,
) -> None:
    from cocoindex.connectors import postgres

    schema = await postgres.TableSchema.from_class(
        CodeEmbedding, primary_key=["id"],
        column_overrides={
            "embedding": postgres.PgType(
                "vector(384)",
                encoder=lambda v: "[" + ",".join(str(x) for x in v) + "]",
            ),
        },
    )
    table = await postgres.mount_table_target(
        PG_POOL, "koku_code_embeddings", schema, pg_schema_name="cocoindex",
    )
    table.declare_vector_index(column="embedding", metric="cosine")

    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql="""
            ALTER TABLE cocoindex.koku_code_embeddings
                ADD COLUMN IF NOT EXISTS search_vector tsvector;

            CREATE INDEX IF NOT EXISTS idx_koku_code_embeddings_fts
                ON cocoindex.koku_code_embeddings USING gin(search_vector);

            CREATE OR REPLACE FUNCTION cocoindex.update_koku_code_search_vector()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_koku_code_search_vector
                ON cocoindex.koku_code_embeddings;
            CREATE TRIGGER trg_koku_code_search_vector
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.koku_code_embeddings
                FOR EACH ROW
                EXECUTE FUNCTION cocoindex.update_koku_code_search_vector();

            UPDATE cocoindex.koku_code_embeddings
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, code, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql="""
            DROP TRIGGER IF EXISTS trg_koku_code_search_vector
                ON cocoindex.koku_code_embeddings;
            DROP FUNCTION IF EXISTS cocoindex.update_koku_code_search_vector();
            DROP INDEX IF EXISTS cocoindex.idx_koku_code_embeddings_fts;
            ALTER TABLE cocoindex.koku_code_embeddings
                DROP COLUMN IF EXISTS search_vector;
        """,
    )

    files = localfs.walk_dir(
        repo_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.py"],
            excluded_patterns=[
                "**/migrations/**",
                "**/tests/**",
                "**/test_*.py",
                "**/node_modules/**",
                "**/.venv/**",
                "**/venv/**",
                "**/vendor/**",
            ],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("koku"),
        process_code_file, files.items(),
        table, repo_dir, "koku",
    )


code_app = coco.App(
    "koku-code", code_main,
    repo_dir=KOKU_REPO_DIR,
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
        t = threading.Thread(target=_run_app, name=f"koku-{name}", daemon=True)
        t.start()
        threads.append(t)

    if "issues" in selected:
        def _issues_poll_loop():
            while True:
                try:
                    log.info("Issues poll: syncing from GitHub...")
                    issues_app.update_blocking()
                    log.info("Issues poll: complete, next in %ds", ISSUES_POLL_INTERVAL)
                except Exception as e:
                    log.error("Issues poll error: %s", e)
                time.sleep(ISSUES_POLL_INTERVAL)

        log.info("Starting issues app (polling every %ds)...", ISSUES_POLL_INTERVAL)
        t = threading.Thread(target=_issues_poll_loop, name="koku-issues", daemon=True)
        t.start()
        threads.append(t)

    log.info("All %d apps launched — docs/code watching files, issues polling every %ds",
             len(threads), ISSUES_POLL_INTERVAL)

    for t in threads:
        t.join()


def main():
    parser = argparse.ArgumentParser(
        description="CocoIndex flows for Koku — incremental ingestion"
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

    log.info("Starting Koku CocoIndex in %s mode — apps: %s", args.mode, ", ".join(sorted(selected)))
    log.info("  Repo dir:            %s", KOKU_REPO_DIR)
    log.info("  Docs dir:            %s", KOKU_DOCS_DIR)
    log.info("  PR repos:            %s", ", ".join(PR_REPOS))
    log.info("  Jira project:        %s", JIRA_PROJECT)
    log.info("  Hindsight URL:       %s", HINDSIGHT_URL)
    log.info("  CocoIndex DB:        %s", COCOINDEX_DB)

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
