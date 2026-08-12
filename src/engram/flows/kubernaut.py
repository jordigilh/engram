#!/usr/bin/env python3
"""CocoIndex flows for Engram — incremental ingestion into Hindsight and pgvector.

Declares four apps:
  1. docs-app:       Markdown docs from all kubernaut repos → Hindsight kubernaut-docs bank
                     (kubernaut-docs, kubernaut/docs, kubernaut-operator/docs,
                      kubernaut-console/docs, kubernaut-demo-scenarios/scenarios+docs)
  2. issues-app:     GitHub issues from all kubernaut repos → Hindsight kubernaut-issues bank
                     (kubernaut, kubernaut-operator, kubernaut-console, kubernaut-demo-scenarios)
  3. code-app:       Go source → pgvector code_embeddings table
  4. transcript-app: Cursor agent transcripts → Hindsight cursor-memory bank

Runs as a single long-lived process via launchd. Supports backfill and live modes.
"""

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import re
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
from engram import correction_gate  # noqa: E402
from engram import contradiction_resolution  # noqa: E402
from engram import project_scope  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("cocoindex-flows")

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")
# Defaults point at the branch-scoped read-only mirrors (see
# watch-mirrors-config.sh), NOT the live dev clones under
# ~/go/src/github.com/jordigilh/ -- see docs/FINDINGS.md 2026-08-03. Watching
# the live clones meant every branch checkout during routine PR work was
# misread as a real content delta, triggering a full hindsight_retain() +
# Sonnet consolidation pass per touched file. These are only defaults (the
# launchd plist sets the same values explicitly); kept in sync so a manual
# `python3 cocoindex-flows.py` invocation without env vars is also safe.
ENGRAM_DOCS_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_DOCS_DIR",
    os.path.expanduser("~/.hindsight/watch/kubernaut-docs/docs"),
))
ENGRAM_CODE_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_CODE_DIR",
    os.path.expanduser("~/.hindsight/watch/kubernaut"),
))
ENGRAM_CODE_DOCS_DIR = ENGRAM_CODE_DIR / "docs"
ENGRAM_OPERATOR_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_OPERATOR_DIR",
    os.path.expanduser("~/.hindsight/watch/kubernaut-operator"),
))
ENGRAM_CONSOLE_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_CONSOLE_DIR",
    os.path.expanduser("~/.hindsight/watch/kubernaut-console"),
))
ENGRAM_SCENARIOS_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_SCENARIOS_DIR",
    os.path.expanduser("~/.hindsight/watch/kubernaut-demo-scenarios"),
))
ENGRAM_TRANSCRIPTS_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_TRANSCRIPTS_DIR",
    os.path.expanduser("~/.cursor/projects"),
))
ISSUES_REPOS = os.environ.get(
    "ENGRAM_ISSUES_REPOS",
    "jordigilh/kubernaut,jordigilh/kubernaut-operator,jordigilh/kubernaut-console,jordigilh/kubernaut-demo-scenarios",
).split(",")
ISSUES_POLL_INTERVAL = int(os.environ.get("ENGRAM_ISSUES_POLL_SECONDS", "300"))

# Manually-curated release lines for the kubernaut family's *code* index only
# -- kept in sync by hand with watch-mirrors-config.sh's RELEASE_LINES (docs
# code_main's release-line loop reads mirror dirs that
# watch-mirrors-config.sh/watch-mirrors-lib.sh are responsible for creating;
# this script never creates or fetches them itself). code_main tags rows
# from these mirrors with repo_tag="{repo}@release-{line}"; docs_main and
# issues_main are unaffected (main-only, unchanged -- see docs/FINDINGS.md
# 2026-08-03 and its 2026-08-10 refinement for why code is cheap to
# multi-branch and docs/issues are not).
KUBERNAUT_RELEASE_LINES = [
    line.strip()
    for line in os.environ.get("KUBERNAUT_RELEASE_LINES", "v1.5,v1.6").split(",")
    if line.strip()
]


def _release_line_dir(repo_name: str, line: str) -> pathlib.Path:
    """Mirror path for one (repo, release line) pair, matching the
    `~/.hindsight/watch/<repo>-release-<line>` convention created by
    watch-mirrors-config.sh's RELEASE_WATCH_MIRRORS."""
    return pathlib.Path(os.path.expanduser(f"~/.hindsight/watch/{repo_name}-release-{line}"))

PG_DSN = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
# asyncpg.create_pool()'s own defaults are min_size=10/max_size=10 -- fine for
# hindsight-api's request-serving pool, wasteful here: PG_POOL only backs
# code_app's pgvector code_embeddings table, written to in occasional bursts
# (initial repo walk, then incremental file-change upserts), never under
# sustained concurrent load. Every onboarded project runs its own
# cocoindex-flows.py process with its own separate PG_POOL against this same
# shared Postgres instance, so an unsized default multiplies per project --
# see docs/FINDINGS.md 2026-08-03. Sized down to match, not eliminated
# (bursts during a full backfill walk do benefit from >1 connection).
PG_POOL_MIN_SIZE = int(os.environ.get("COCOINDEX_PG_POOL_MIN_SIZE", "2"))
PG_POOL_MAX_SIZE = int(os.environ.get("COCOINDEX_PG_POOL_MAX_SIZE", "5"))
COCOINDEX_DB = pathlib.Path(os.environ.get(
    "COCOINDEX_DB",
    os.path.expanduser("~/.hindsight/cocoindex.db"),
))

# Per-transcript watermark (message_count already scanned) for the live
# transcript-app flow -- see docs/FINDINGS.md 2026-07-31. CocoIndex's
# live=True file-watcher re-triggers process_transcript() with the file's
# FULL current content on every write, not just the delta, so without this
# the whole transcript (including corrections already extracted and
# resolved/queued many times before) got re-scanned and re-checked against
# Hindsight on every single new message in an active session -- one real
# contradiction was queued 104 times over 3 days from a single long-lived
# session before pending_queue.append_pending() grew its own dedup guard.
# This watermark stops the re-extraction/re-check at the source instead of
# only de-duplicating its symptom. Mirrors nightly-learn.py's
# watermarks.json (size + message_count), but kept in its own file: the two
# flows track independent progress (nightly-learn.py's hourly/nightly sweep
# is a separate catch-all, not the same consumer), so sharing one file would
# let either one silently skip content the other never actually processed.
TRANSCRIPT_WATERMARKS_PATH = pathlib.Path(os.path.expanduser(
    "~/.hindsight/logs/cocoindex-transcript-watermarks.json"
))
_transcript_watermarks_lock = asyncio.Lock()


def _load_transcript_watermarks() -> dict:
    if TRANSCRIPT_WATERMARKS_PATH.exists():
        try:
            return json.loads(TRANSCRIPT_WATERMARKS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt cocoindex-transcript-watermarks.json, starting fresh")
    return {}


def _save_transcript_watermarks(watermarks: dict) -> None:
    TRANSCRIPT_WATERMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TRANSCRIPT_WATERMARKS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(watermarks, indent=2))
    tmp.rename(TRANSCRIPT_WATERMARKS_PATH)


PG_POOL: coco.ContextKey[Any] = coco.ContextKey("pg_pool")

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}

CORRECTION_PATTERNS = [
    re.compile(r"\bno[,.]?\s+that'?s\s+(not|wrong|incorrect)", re.I),
    re.compile(r"\bdon'?t\s+do\s+that", re.I),
    re.compile(r"\bI\s+(said|meant)\s+", re.I),
    re.compile(r"\bwrong\s+(file|path|dir|approach|method|function|model|endpoint)", re.I),
    re.compile(r"\bthat\s+broke", re.I),
    re.compile(r"\bundo\s+(that|this|it)", re.I),
    re.compile(r"\bthat'?s\s+not\s+what\s+I", re.I),
    re.compile(r"\byou\s+(shouldn'?t|should\s+not)\s+have", re.I),
    re.compile(r"\bdo\s+not\s+use\b", re.I),
    re.compile(r"\bwe\s+don'?t\s+use\b", re.I),
    # Keep in sync with the same list in nightly-learn.py/report.py. Added
    # 2026-07-08 — see docs/FINDINGS.md for why (16 real corrections/7 days,
    # 0 detected; this copy is the one that decides what gets tagged
    # [CORRECTION] for cursor-memory bank ingestion, so it's the most
    # consequential of the three to have fixed).
    re.compile(r"\b(you'?re|you\s+are)\s+(still\s+)?not\s+(following|aligned)\b", re.I),
    re.compile(r"\bnot\s+following\s+(the\s+)?(project'?s?\s+)?(methodology|convention|AGENTS\.md|CLAUDE\.md)\b", re.I),
    re.compile(r"\byou\s+keep\s+making\s+the\s+same\s+mistake\b", re.I),
    re.compile(r"\bmistak(?:e|ing)\b.{0,40}\bfor\b", re.I),
]

INSTRUCTION_PATTERNS = [
    re.compile(r"\balways\s+(use|follow|run|start\s+with|ensure)", re.I),
    re.compile(r"\bnever\s+(skip|push|commit|deploy|use)", re.I),
    re.compile(r"\bmandatory\b", re.I),
    re.compile(r"\bour\s+(workflow|process|methodology|convention|standard)", re.I),
    re.compile(r"\bthe\s+rule\s+is\b", re.I),
    re.compile(r"\bfor\s+this\s+(project|repo|team)\s+we\b", re.I),
    re.compile(r"\bwe\s+(always|never|require|must)\b", re.I),
    re.compile(r"\bbefore\s+(implementing|proceeding|starting\s+any)", re.I),
]


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _wait_for_hindsight(max_retries: int = 30, delay: float = 2.0) -> None:
    """Block until the Hindsight API responds to a health check."""
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
    """Push a memory to Hindsight via the retain API.

    Previously always passed strategy="exact", but hindsight-api only
    applies a named retain strategy if it's registered in the bank's own
    config -- "exact" never was, so it silently no-op'd back to the bank's
    default resolved config on every call (confirmed by reading
    hindsight-api's config_resolver.apply_named_retain_strategy(), which
    logs a warning and returns the config unchanged for unknown names). The
    bank's default delta-retain already skips extraction/consolidation
    entirely when a chunk's content hash is unchanged, so the strategy
    override added no behavior, only log noise. See docs/FINDINGS.md
    2026-08-03.
    """
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
# App 1: docs-app — Markdown docs → Hindsight kubernaut-docs bank
# ---------------------------------------------------------------------------

@coco.fn(memo=True)
async def process_doc_file(
    file: localfs.File,
    base_dir: pathlib.Path,
    source_tag: str,
) -> None:
    """Read a markdown file, chunk it, and push to Hindsight."""
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
        if source_tag == "kubernaut-repo":
            chunk = f"[Source: kubernaut repo docs — verify against published docs if conflicting]\n\n{chunk}"

        doc_id = base_doc_id if not key else f"{base_doc_id}--{key}"
        hindsight_retain(
            bank_id="kubernaut-docs",
            content=chunk,
            document_id=doc_id,
            timestamp=timestamp,
            metadata={"source": "cocoindex", "repo": source_tag},
            tags=[section, source_tag],
        )


@coco.fn
async def docs_main(
    docs_dir: pathlib.Path,
    code_docs_dir: pathlib.Path,
    operator_dir: pathlib.Path,
    console_dir: pathlib.Path,
    scenarios_dir: pathlib.Path,
) -> None:
    """Walk markdown docs from all kubernaut repos and mount one component per file."""
    published = localfs.walk_dir(
        docs_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*.md"]),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("published"),
        process_doc_file, published.items(),
        docs_dir, "kubernaut-docs-repo",
    )

    dev_docs = localfs.walk_dir(
        code_docs_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=[
                "architecture/**/*.md",
                "design/**/*.md",
                "development/**/*.md",
                "operations/**/*.md",
                "requirements/**/*.md",
                "spikes/**/*.md",
                "testing/**/*.md",
                "tests/**/*.md",
            ],
            excluded_patterns=["**/generated/**", "**/presentations/**"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("dev-docs"),
        process_doc_file, dev_docs.items(),
        code_docs_dir, "kubernaut-repo",
    )

    operator_docs = localfs.walk_dir(
        operator_dir / "docs",
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.md"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("operator-docs"),
        process_doc_file, operator_docs.items(),
        operator_dir / "docs", "kubernaut-operator",
    )

    console_docs = localfs.walk_dir(
        console_dir / "docs",
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.md"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("console-docs"),
        process_doc_file, console_docs.items(),
        console_dir / "docs", "kubernaut-console",
    )

    scenarios_docs = localfs.walk_dir(
        scenarios_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=[
                "scenarios/*/README.md",
                "docs/**/*.md",
                "reports/**/*.md",
                "README.md",
            ],
            excluded_patterns=[
                "overnight-logs-*/**",
                "parallel-results-*/**",
                "rerun-*/**",
                "redeploy-*/**",
                "sequential-*/**",
                "golden-transcripts/**",
            ],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("scenarios-docs"),
        process_doc_file, scenarios_docs.items(),
        scenarios_dir, "kubernaut-demo-scenarios",
    )


docs_app = coco.App(
    "engram-docs", docs_main,
    docs_dir=ENGRAM_DOCS_DIR,
    code_docs_dir=ENGRAM_CODE_DOCS_DIR,
    operator_dir=ENGRAM_OPERATOR_DIR,
    console_dir=ENGRAM_CONSOLE_DIR,
    scenarios_dir=ENGRAM_SCENARIOS_DIR,
)


# ---------------------------------------------------------------------------
# App 2: issues-app — GitHub Issues → Hindsight kubernaut-issues bank
# ---------------------------------------------------------------------------

def _fetch_all_issues(repo: str) -> list[dict]:
    """Fetch all issues (including closed) and PRs from GitHub via `gh` CLI.

    The `gh` CLI handles GraphQL pagination internally — setting a high
    --limit is enough to retrieve everything.
    """
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


def _repo_short_name(repo: str) -> str:
    """Extract a short name from a GitHub repo slug (e.g. 'jordigilh/kubernaut-operator' -> 'kubernaut-operator')."""
    return repo.split("/")[-1] if "/" in repo else repo


def _format_issue_header(issue: dict, repo: str) -> str:
    """Format an issue/PR's static header + description.

    Deliberately excludes state/labels -- those change routinely (PRs get
    merged, labels get triaged) and are already carried in the retain
    call's `metadata`/`tags`. Including them here used to make every
    state/label change look like a content edit to hindsight-api's
    delta-diff, invalidating this chunk's stored hash (and, since it used
    to be prepended to the *whole* concatenated issue+comments text, every
    comment chunk after it too) for reasons unrelated to the actual issue
    content. See docs/FINDINGS.md 2026-08-03.
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
def process_issue(issue: dict, repo: str) -> None:
    """Format, chunk, and push a single issue to Hindsight.

    Chunked as one section for the header+description plus one section per
    comment (chunking.split_issue_sections()), each keyed by the comment's
    own ordinal position rather than a character offset into the whole
    concatenated text -- comments are append-only, so a new comment only
    ever adds a new key/chunk at the end instead of reindexing every
    existing comment. See docs/FINDINGS.md 2026-08-03.
    """
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
            bank_id="kubernaut-issues",
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
    """Fetch all issues from all repos and process each one."""
    for repo in repos.split(","):
        repo = repo.strip()
        if not repo:
            continue
        issues = _fetch_all_issues(repo)
        log.info("Fetched %d issues from %s", len(issues), repo)
        for issue in issues:
            process_issue(issue, repo)


issues_app = coco.App("engram-issues", issues_main, repos=",".join(ISSUES_REPOS))


# ---------------------------------------------------------------------------
# App 3: code-app — Go source → pgvector code_embeddings table
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CodeEmbedding:
    id: str
    filepath: str
    chunk_index: int
    code: str
    embedding: list[float]
    search_text: str  # concatenated text for BM25 full-text search


@coco.fn(memo=True)
async def process_code_file(
    file: localfs.File,
    table: "Any",
    base_dir: pathlib.Path,
    repo_tag: str,
) -> None:
    """Read a source file, chunk it, embed, and declare rows in pgvector table."""

    content = await file.read_text()
    if not content or not content.strip():
        return

    abs_path = str(file.file_path.resolve())
    base_prefix = str(base_dir) + "/"
    rel_path = abs_path.replace(base_prefix, "") if abs_path.startswith(base_prefix) else str(file.file_path.path)
    filepath = f"{repo_tag}/{rel_path}"

    chunks = chunking.split_code(content, filename=filepath, chunk_size=1000, chunk_overlap=300)

    # Concurrent embed() calls via chunking.embed_code_chunks() let
    # cocoindex's SentenceTransformerEmbedder batch this file's chunks into
    # one model.encode() call instead of one sequential call per chunk --
    # see docs/FINDINGS.md 2026-08-07.
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
async def code_main(
    code_dir: pathlib.Path,
    operator_dir: pathlib.Path,
    console_dir: pathlib.Path,
) -> None:
    """Walk source files from kubernaut repos, embed, and store in pgvector."""
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
        PG_POOL, "code_embeddings", schema, pg_schema_name="cocoindex",
    )
    table.declare_vector_index(column="embedding", metric="cosine")

    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql="""
            ALTER TABLE cocoindex.code_embeddings
                ADD COLUMN IF NOT EXISTS search_vector tsvector;

            CREATE INDEX IF NOT EXISTS idx_code_embeddings_fts
                ON cocoindex.code_embeddings USING gin(search_vector);

            CREATE OR REPLACE FUNCTION cocoindex.update_code_search_vector()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_code_search_vector
                ON cocoindex.code_embeddings;
            CREATE TRIGGER trg_code_search_vector
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.code_embeddings
                FOR EACH ROW
                EXECUTE FUNCTION cocoindex.update_code_search_vector();

            UPDATE cocoindex.code_embeddings
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, code, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql="""
            DROP TRIGGER IF EXISTS trg_code_search_vector
                ON cocoindex.code_embeddings;
            DROP FUNCTION IF EXISTS cocoindex.update_code_search_vector();
            DROP INDEX IF EXISTS cocoindex.idx_code_embeddings_fts;
            ALTER TABLE cocoindex.code_embeddings
                DROP COLUMN IF EXISTS search_vector;
        """,
    )

    kubernaut_files = localfs.walk_dir(
        code_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.go"],
            excluded_patterns=["**/vendor/**", "**/*_test.go", "**/zz_generated*"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("kubernaut"),
        process_code_file, kubernaut_files.items(),
        table, code_dir, "kubernaut",
    )

    operator_files = localfs.walk_dir(
        operator_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.go"],
            excluded_patterns=["**/vendor/**", "**/*_test.go", "**/zz_generated*"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("kubernaut-operator"),
        process_code_file, operator_files.items(),
        table, operator_dir, "kubernaut-operator",
    )

    console_files = localfs.walk_dir(
        console_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.ts", "**/*.tsx"],
            excluded_patterns=["**/node_modules/**", "**/dist/**", "**/storybook-static/**", "**/*.d.ts"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("kubernaut-console"),
        process_code_file, console_files.items(),
        table, console_dir, "kubernaut-console",
    )

    # Release-line mirrors (main is the 3 blocks above) -- see
    # watch-mirrors-config.sh's RELEASE_WATCH_MIRRORS and KUBERNAUT_RELEASE_LINES
    # above. A line with no mirror dir yet (e.g. release/v1.6 before it's cut
    # upstream) is skipped gracefully; watch-mirrors-lib.sh logs its own INFO
    # when that happens, so no duplicate warning is needed here.
    release_repos = [
        ("kubernaut", code_dir, ["**/*.go"], ["**/vendor/**", "**/*_test.go", "**/zz_generated*"]),
        ("kubernaut-operator", operator_dir, ["**/*.go"], ["**/vendor/**", "**/*_test.go", "**/zz_generated*"]),
        ("kubernaut-console", console_dir, ["**/*.ts", "**/*.tsx"],
         ["**/node_modules/**", "**/dist/**", "**/storybook-static/**", "**/*.d.ts"]),
    ]
    for repo_name, _main_dir, included, excluded in release_repos:
        for line in KUBERNAUT_RELEASE_LINES:
            line_dir = _release_line_dir(repo_name, line)
            if not line_dir.is_dir():
                log.info(
                    "code_main: skipping %s@release-%s -- mirror not present at %s yet",
                    repo_name, line, line_dir,
                )
                continue
            repo_tag = f"{repo_name}@release-{line}"
            release_files = localfs.walk_dir(
                line_dir,
                recursive=True,
                path_matcher=PatternFilePathMatcher(
                    included_patterns=included,
                    excluded_patterns=excluded,
                ),
                live=True,
            )
            await coco.mount_each(
                coco.component_subpath(repo_tag),
                process_code_file, release_files.items(),
                table, line_dir, repo_tag,
            )


code_app = coco.App(
    "engram-code", code_main,
    code_dir=ENGRAM_CODE_DIR,
    operator_dir=ENGRAM_OPERATOR_DIR,
    console_dir=ENGRAM_CONSOLE_DIR,
)


# ---------------------------------------------------------------------------
# App 4: transcript-app — Agent transcripts → Hindsight cursor-memory bank
# ---------------------------------------------------------------------------

def _is_correction(text: str) -> bool:
    """Delegates to correction_gate (Haiku-based, disk-cached -- see
    docs/FINDINGS.md). Set ENGRAM_CORRECTION_DETECTOR=regex for an instant
    rollback to CORRECTION_PATTERNS below, which is kept in place unused.
    """
    return correction_gate.is_correction(text, CORRECTION_PATTERNS)


def _is_instruction(text: str) -> bool:
    if not text or len(text) < 20 or len(text) > 2000:
        return False
    return any(pat.search(text) for pat in INSTRUCTION_PATTERNS)


def _extract_user_text(msg: dict) -> str:
    content = msg.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
            if match:
                texts.append(match.group(1))
            elif not text.startswith("<external_links>"):
                texts.append(text)
    raw = "\n".join(texts).strip()
    if correction_gate.is_system_boilerplate(raw):
        return ""
    return raw


def _extract_assistant_text(msg: dict) -> str:
    content = msg.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return "\n".join(texts).strip()


def _extract_learning_windows(messages: list[dict], window: int = 2, start_index: int = 0) -> list[str]:
    """Extract 5-message learning windows around corrections/instructions.

    start_index: only emit windows for signal messages at raw `messages`
    indices >= start_index (already-scanned messages, per the caller's
    watermark) -- context is still drawn from the full `messages` list so a
    new signal right at the watermark boundary still gets proper
    before-context. See docs/FINDINGS.md 2026-07-31 and
    nightly-learn.py's extract_learning_windows(), which this mirrors.
    """
    parsed = []
    for raw_idx, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "user":
            text = _extract_user_text(msg)
            if text:
                parsed.append({
                    "role": "user", "text": text,
                    "is_correction": _is_correction(text),
                    "is_instruction": _is_instruction(text),
                    "raw_idx": raw_idx,
                })
        elif role == "assistant":
            text = _extract_assistant_text(msg)
            if text:
                parsed.append({
                    "role": "assistant", "text": text[:400],
                    "is_correction": False, "is_instruction": False,
                    "raw_idx": raw_idx,
                })

    signal_indices = [
        i for i, m in enumerate(parsed)
        if (m["is_correction"] or m["is_instruction"]) and m["raw_idx"] >= start_index
    ]

    windows = []
    used: set[int] = set()
    for idx in signal_indices:
        if idx in used:
            continue
        used.add(idx)
        start = max(0, idx - window)
        end = min(len(parsed), idx + window + 1)
        lines = []
        for i in range(start, end):
            m = parsed[i]
            tag = ""
            if i == idx:
                tag = "[CORRECTION] " if m["is_correction"] else "[INSTRUCTION] "
            lines.append(f"{tag}{m['role'].title()}: {m['text'][:300]}")
        windows.append("\n\n".join(lines))

    return windows


def _window_document_id(transcript_id: str, window_text: str) -> str:
    """Stable, content-derived document id for a learning window.

    Deliberately NOT based on the window's position in the (now
    watermark-sliced, see below) `windows` list: once process_transcript()
    only extracts *new* windows per invocation, a positional index would
    restart at 0 on every call and silently collide with -- overwriting --
    an unrelated, already-retained window from an earlier call. A digest of
    the window's own text is stable regardless of which batch it was
    extracted in and only collides on identical content (which is the
    correct, idempotent behavior: memo=True style de-dup, not data loss).
    """
    digest = hashlib.sha1(window_text.encode("utf-8")).hexdigest()[:16]
    return f"transcript-{transcript_id}-w{digest}"


@coco.fn(memo=True)
async def process_transcript(file: localfs.File) -> None:
    """Parse a JSONL transcript and push learning windows to Hindsight."""
    content = await file.read_text()
    if not content or not content.strip():
        return

    messages = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not messages:
        return

    transcript_id = file.file_path.path.stem

    # Watermark gate -- see docs/FINDINGS.md 2026-07-31. CocoIndex's
    # live=True watcher re-delivers the transcript's FULL current content on
    # every write (not a diff), so without tracking how many messages we've
    # already scanned for THIS transcript_id, every new message in a
    # long-lived session re-triggers is_correction()/is_instruction()
    # classification and (for corrections) a fresh Hindsight contradiction
    # check against the WHOLE history again, not just the new tail.
    async with _transcript_watermarks_lock:
        prev_count = _load_transcript_watermarks().get(transcript_id, {}).get("message_count", 0)

    # A transcript can only grow (messages are appended, never rewritten in
    # place), but guard against a shrunk/rewritten file anyway by clamping
    # to 0 rather than passing a start_index past the end of `messages`.
    start_index = prev_count if prev_count <= len(messages) else 0

    windows = _extract_learning_windows(messages, start_index=start_index)

    # Same pattern as the other flows in this file (see e.g. rel_path
    # computation above): file.file_path.path is only the *relative* path
    # (a PurePosixPath -- no .resolve()); file.file_path.resolve() is what
    # gives the absolute concrete Path. Mixing these up (as an earlier
    # version of this fix did) throws AttributeError in production the
    # instant a real cocoindex.localfs.File hits this code, since the unit
    # test's FakeFile mock didn't model the distinction -- see docs/FINDINGS.md.
    abs_path = str(file.file_path.resolve())
    base_prefix = str(ENGRAM_TRANSCRIPTS_DIR) + "/"
    project = None
    if abs_path.startswith(base_prefix):
        project_dir_name = abs_path[len(base_prefix):].split("/", 1)[0]
        project = project_scope.resolve_project_label(project_dir_name)

    for window_text in windows:
        if not window_text.strip():
            continue
        # tags: [project] if known, same as nightly-learn.py's retain_windows()
        # (see docs/FINDINGS.md 2026-07-27 -- that fix only touched
        # retain_windows(); this parallel CocoIndex-side retain path kept
        # writing untagged facts to cursor-memory for another day of live
        # traffic until this mirror fix).
        tags: list[str] = [project] if project else []
        if "[CORRECTION]" in window_text:
            resolution = contradiction_resolution.resolve("cursor-memory", window_text, project=project)
            if resolution.action == "auto_resolved":
                tags += ["CORRECTION", "supersedes-prior-memory"]
            elif resolution.action == "queued":
                # Withheld from retain pending human review (pending_queue.py's
                # contract: "never auto-retained"). review-contradictions.py
                # retains it itself on approve. See docs/FINDINGS.md.
                continue
        hindsight_retain(
            bank_id="cursor-memory",
            content=window_text,
            document_id=_window_document_id(transcript_id, window_text),
            metadata={"source": "cocoindex-transcript", "transcript_id": transcript_id},
            tags=tags or None,
        )

    # Advance the watermark past everything we just scanned, whether or not
    # it produced any windows -- mirrors nightly-learn.py's filter_and_scan(),
    # which updates the watermark "regardless (we've scanned these
    # messages)" so a signal-free tail doesn't get rescanned forever either.
    if len(messages) > prev_count:
        async with _transcript_watermarks_lock:
            watermarks = _load_transcript_watermarks()
            watermarks[transcript_id] = {
                "message_count": len(messages),
                "last_processed": datetime.now().isoformat(),
            }
            _save_transcript_watermarks(watermarks)


@coco.fn
async def transcript_main(transcripts_dir: pathlib.Path) -> None:
    """Walk JSONL transcripts and mount one component per file.

    Scoped to onboarded projects only (project_scope.py) -- see
    docs/FINDINGS.md 2026-07-13. Before this, included_patterns=["**/*.jsonl"]
    matched every one of ~270 Cursor workspaces under transcripts_dir, not
    just kubernaut/dcm/engram.
    """
    files = localfs.walk_dir(
        transcripts_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=project_scope.transcript_glob_patterns()
        ),
        live=True,
    )
    await coco.mount_each(process_transcript, files.items())


transcript_app = coco.App("engram-transcripts", transcript_main, transcripts_dir=ENGRAM_TRANSCRIPTS_DIR)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _run_live(selected: set[str]) -> None:
    """Run all selected apps concurrently in live mode using threads.

    File-watching apps (docs, code, transcripts) use CocoIndex live mode.
    Issues polls GitHub on ISSUES_POLL_INTERVAL since there's no filesystem to watch.
    """
    import threading

    threads: list[threading.Thread] = []

    for name, app in [("docs", docs_app), ("code", code_app), ("transcripts", transcript_app)]:
        if name not in selected:
            continue
        def _run_app(n=name, a=app):
            log.info("Starting %s-app (live, file-watching)...", n)
            try:
                a.update_blocking(live=True)
            except Exception as e:
                log.error("%s-app crashed: %s", n, e)
        t = threading.Thread(target=_run_app, name=f"cocoindex-{name}", daemon=True)
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

        log.info("Starting issues-app (polling every %ds)...", ISSUES_POLL_INTERVAL)
        t = threading.Thread(target=_issues_poll_loop, name="cocoindex-issues", daemon=True)
        t.start()
        threads.append(t)

    log.info("All %d apps launched — docs/code/transcripts watching files, issues polling every %ds",
             len(threads), ISSUES_POLL_INTERVAL)

    for t in threads:
        t.join()


def main():
    parser = argparse.ArgumentParser(
        description="CocoIndex flows for Engram — incremental ingestion"
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
        choices=["docs", "issues", "code", "transcripts"],
        default=None,
        help="Run only specific apps (default: all)",
    )
    args = parser.parse_args()

    selected = set(args.apps) if args.apps else {"docs", "issues", "code", "transcripts"}

    log.info("Starting CocoIndex in %s mode — apps: %s", args.mode, ", ".join(sorted(selected)))
    log.info("  Docs dir:        %s", ENGRAM_DOCS_DIR)
    log.info("  Code dir:        %s", ENGRAM_CODE_DIR)
    log.info("  Operator dir:    %s", ENGRAM_OPERATOR_DIR)
    log.info("  Console dir:     %s", ENGRAM_CONSOLE_DIR)
    log.info("  Scenarios dir:   %s", ENGRAM_SCENARIOS_DIR)
    log.info("  Transcripts dir: %s", ENGRAM_TRANSCRIPTS_DIR)
    log.info("  Issues repos:    %s", ", ".join(ISSUES_REPOS))
    log.info("  Hindsight URL:   %s", HINDSIGHT_URL)
    log.info("  CocoIndex DB:    %s", COCOINDEX_DB)

    _wait_for_hindsight()

    if args.mode == "backfill":
        for name, app in [("docs", docs_app), ("issues", issues_app), ("code", code_app), ("transcripts", transcript_app)]:
            if name in selected:
                log.info("Running %s-app backfill...", name)
                app.update_blocking(report_to_stdout=True)
                log.info("%s-app backfill complete", name)
        log.info("All backfills finished")
    else:
        _run_live(selected)


if __name__ == "__main__":
    main()
