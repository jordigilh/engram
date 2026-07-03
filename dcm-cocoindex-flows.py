#!/usr/bin/env python3
"""CocoIndex flows for DCM — incremental ingestion into Hindsight and pgvector.

Declares three apps:
  1. dcm-docs:   Markdown docs from DCM repos → Hindsight dcm-docs bank
  2. dcm-issues: GitHub issues from DCM repos → Hindsight dcm-issues bank
  3. dcm-code:   Go/Shell/YAML source → pgvector dcm_code_embeddings table

Runs as a single long-lived process via launchd. Supports backfill and live modes.
"""

import argparse
import dataclasses
import json
import logging
import os
import pathlib
import re
import subprocess
import time
from datetime import datetime
from typing import Any, AsyncIterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import cocoindex as coco
from cocoindex.connectors import localfs
from cocoindex.resources.file import PatternFilePathMatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("dcm-cocoindex-flows")

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")

DCM_ARCHITECTURE_DIR = pathlib.Path(os.environ.get(
    "DCM_ARCHITECTURE_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/dcm"),
))
DCM_DOCS_DIR = pathlib.Path(os.environ.get(
    "DCM_DOCS_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/dcm-project.github.io"),
))
DCM_ENHANCEMENTS_DIR = pathlib.Path(os.environ.get(
    "DCM_ENHANCEMENTS_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/enhancements"),
))
DCM_CONTROL_PLANE_DIR = pathlib.Path(os.environ.get(
    "DCM_CONTROL_PLANE_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/control-plane"),
))
DCM_CLI_DIR = pathlib.Path(os.environ.get(
    "DCM_CLI_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/cli"),
))
DCM_KUBEVIRT_SP_DIR = pathlib.Path(os.environ.get(
    "DCM_KUBEVIRT_SP_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/kubevirt-service-provider"),
))
DCM_K8S_CONTAINER_SP_DIR = pathlib.Path(os.environ.get(
    "DCM_K8S_CONTAINER_SP_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/k8s-container-service-provider"),
))
DCM_ACM_CLUSTER_SP_DIR = pathlib.Path(os.environ.get(
    "DCM_ACM_CLUSTER_SP_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/acm-cluster-service-provider"),
))
DCM_THREE_TIER_SP_DIR = pathlib.Path(os.environ.get(
    "DCM_THREE_TIER_SP_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/three-tier-app-demo-service-provider"),
))
DCM_UTILITIES_DIR = pathlib.Path(os.environ.get(
    "DCM_UTILITIES_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/utilities"),
))
DCM_SHARED_WORKFLOWS_DIR = pathlib.Path(os.environ.get(
    "DCM_SHARED_WORKFLOWS_DIR",
    os.path.expanduser("~/go/src/github.com/dcm-project/shared-workflows"),
))

ISSUES_REPOS = os.environ.get(
    "DCM_ISSUES_REPOS",
    "dcm-project/dcm,dcm-project/control-plane,dcm-project/cli,"
    "dcm-project/kubevirt-service-provider,dcm-project/k8s-container-service-provider,"
    "dcm-project/acm-cluster-service-provider,dcm-project/three-tier-app-demo-service-provider,"
    "dcm-project/utilities,dcm-project/dcm-project.github.io,dcm-project/enhancements,"
    "dcm-project/shared-workflows,dcm-project/quadlet-deploy",
).split(",")
ISSUES_POLL_INTERVAL = int(os.environ.get("DCM_ISSUES_POLL_SECONDS", "300"))

PG_DSN = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
COCOINDEX_DB = pathlib.Path(os.environ.get(
    "COCOINDEX_DB",
    os.path.expanduser("~/.hindsight/dcm-cocoindex.db"),
))

PG_POOL: coco.ContextKey[Any] = coco.ContextKey("pg_pool")

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
        "strategy": "exact",
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
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end < len(text):
            break_point = text.rfind("\n", start, end)
            if break_point > start + chunk_size // 2:
                end = break_point + 1
        chunks.append(text[start:end])
        start = end - chunk_overlap
    return chunks


# ---------------------------------------------------------------------------
# Lifespan: configure CocoIndex database path + Postgres pool for code index
# ---------------------------------------------------------------------------

@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    from cocoindex.connectors import postgres

    builder.settings.db_path = COCOINDEX_DB
    pool = await postgres.create_pool(PG_DSN)
    builder.provide(PG_POOL, pool)
    yield
    pool.close()


# ---------------------------------------------------------------------------
# App 1: dcm-docs — Markdown docs → Hindsight dcm-docs bank
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

    chunks = _split_text(content, chunk_size=800, chunk_overlap=200)
    for i, chunk in enumerate(chunks):
        doc_id = f"{source_tag}--{rel_path.replace('/', '--').replace('.md', '')}"
        if i > 0:
            doc_id = f"{doc_id}--chunk{i}"
        hindsight_retain(
            bank_id="dcm-docs",
            content=chunk,
            document_id=doc_id,
            timestamp=timestamp,
            metadata={"source": "cocoindex", "repo": source_tag},
            tags=[section, source_tag],
        )


@coco.fn
async def docs_main(
    architecture_dir: pathlib.Path,
    docs_dir: pathlib.Path,
    enhancements_dir: pathlib.Path,
    control_plane_dir: pathlib.Path,
    cli_dir: pathlib.Path,
    acm_cluster_sp_dir: pathlib.Path,
    k8s_container_sp_dir: pathlib.Path,
    utilities_dir: pathlib.Path,
) -> None:
    architecture_docs = localfs.walk_dir(
        architecture_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=[
                "architecture/**/*.md",
                "taxonomy/**/*.md",
                "scope/**/*.md",
                "overview.md",
            ],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("architecture"),
        process_doc_file, architecture_docs.items(),
        architecture_dir, "dcm-architecture",
    )

    website_docs = localfs.walk_dir(
        docs_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["content/**/*.md"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("docs-website"),
        process_doc_file, website_docs.items(),
        docs_dir, "dcm-docs-website",
    )

    enhancement_docs = localfs.walk_dir(
        enhancements_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["enhancements/**/*.md"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("enhancements"),
        process_doc_file, enhancement_docs.items(),
        enhancements_dir, "dcm-enhancements",
    )

    cp_docs = localfs.walk_dir(
        control_plane_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.md"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("control-plane-docs"),
        process_doc_file, cp_docs.items(),
        control_plane_dir, "dcm-control-plane",
    )

    cli_docs = localfs.walk_dir(
        cli_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=[
                ".ai/**/*.md",
                "CLAUDE.md",
                "README.md",
            ],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("cli-docs"),
        process_doc_file, cli_docs.items(),
        cli_dir, "dcm-cli",
    )

    acm_docs = localfs.walk_dir(
        acm_cluster_sp_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=[
                ".ai/**/*.md",
                "CLAUDE.md",
                "README.md",
                "RUN.md",
            ],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("acm-cluster-sp-docs"),
        process_doc_file, acm_docs.items(),
        acm_cluster_sp_dir, "dcm-acm-cluster-sp",
    )

    k8s_sp_docs = localfs.walk_dir(
        k8s_container_sp_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=[
                ".ai/**/*.md",
                "CLAUDE.md",
                "README.md",
            ],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("k8s-container-sp-docs"),
        process_doc_file, k8s_sp_docs.items(),
        k8s_container_sp_dir, "dcm-k8s-container-sp",
    )

    utilities_docs = localfs.walk_dir(
        utilities_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=[
                "test-plans/**/*.md",
                ".cursor/prompts/**/*.md",
                "CLAUDE.md",
                "README.md",
            ],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("utilities-docs"),
        process_doc_file, utilities_docs.items(),
        utilities_dir, "dcm-utilities",
    )


docs_app = coco.App(
    "dcm-docs", docs_main,
    architecture_dir=DCM_ARCHITECTURE_DIR,
    docs_dir=DCM_DOCS_DIR,
    enhancements_dir=DCM_ENHANCEMENTS_DIR,
    control_plane_dir=DCM_CONTROL_PLANE_DIR,
    cli_dir=DCM_CLI_DIR,
    acm_cluster_sp_dir=DCM_ACM_CLUSTER_SP_DIR,
    k8s_container_sp_dir=DCM_K8S_CONTAINER_SP_DIR,
    utilities_dir=DCM_UTILITIES_DIR,
)


# ---------------------------------------------------------------------------
# App 2: dcm-issues — GitHub Issues → Hindsight dcm-issues bank
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


def _repo_short_name(repo: str) -> str:
    return repo.split("/")[-1] if "/" in repo else repo


def _format_issue_content(issue: dict, repo: str) -> str:
    parts = []
    number = issue.get("number", "?")
    title = issue.get("title", "")
    state = issue.get("state", "OPEN")
    kind = issue.get("_kind", "issue")
    kind_label = "PR" if kind == "pr" else "Issue"
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    author = issue.get("author", {}).get("login", "unknown")
    created = issue.get("createdAt", "")[:10]
    short_repo = _repo_short_name(repo)

    parts.append(f"# {kind_label} #{number} ({short_repo}): {title}")
    parts.append(f"Repo: {repo} | State: {state} | Labels: {', '.join(labels) or 'none'} | Author: {author} | Created: {created}")
    parts.append("")

    body = issue.get("body", "") or ""
    if body.strip():
        parts.append(body.strip())
        parts.append("")

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


@coco.fn(memo=True)
def process_issue(issue: dict, repo: str) -> None:
    content = _format_issue_content(issue, repo)
    if not content.strip() or len(content) < 50:
        return

    number = issue.get("number", 0)
    updated_at = issue.get("updatedAt", "")
    state = issue.get("state", "OPEN").lower()
    kind = issue.get("_kind", "issue")
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    short_repo = _repo_short_name(repo)

    chunks = _split_text(content, chunk_size=1200, chunk_overlap=300)
    for i, chunk in enumerate(chunks):
        doc_id = f"{short_repo}-{kind}-{number}"
        if i > 0:
            doc_id = f"{short_repo}-{kind}-{number}-chunk{i}"
        hindsight_retain(
            bank_id="dcm-issues",
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


issues_app = coco.App("dcm-issues", issues_main, repos=",".join(ISSUES_REPOS))


# ---------------------------------------------------------------------------
# App 3: dcm-code — Go/Shell/YAML source → pgvector dcm_code_embeddings table
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
    from cocoindex.connectors import postgres

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
    control_plane_dir: pathlib.Path,
    cli_dir: pathlib.Path,
    kubevirt_sp_dir: pathlib.Path,
    k8s_container_sp_dir: pathlib.Path,
    acm_cluster_sp_dir: pathlib.Path,
    three_tier_sp_dir: pathlib.Path,
    utilities_dir: pathlib.Path,
    shared_workflows_dir: pathlib.Path,
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
        PG_POOL, "dcm_code_embeddings", schema, pg_schema_name="cocoindex",
    )
    table.declare_vector_index(column="embedding", metric="cosine")

    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql="""
            ALTER TABLE cocoindex.dcm_code_embeddings
                ADD COLUMN IF NOT EXISTS search_vector tsvector;

            CREATE INDEX IF NOT EXISTS idx_dcm_code_embeddings_fts
                ON cocoindex.dcm_code_embeddings USING gin(search_vector);

            CREATE OR REPLACE FUNCTION cocoindex.update_dcm_code_search_vector()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_dcm_code_search_vector
                ON cocoindex.dcm_code_embeddings;
            CREATE TRIGGER trg_dcm_code_search_vector
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.dcm_code_embeddings
                FOR EACH ROW
                EXECUTE FUNCTION cocoindex.update_dcm_code_search_vector();

            UPDATE cocoindex.dcm_code_embeddings
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, code, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql="""
            DROP TRIGGER IF EXISTS trg_dcm_code_search_vector
                ON cocoindex.dcm_code_embeddings;
            DROP FUNCTION IF EXISTS cocoindex.update_dcm_code_search_vector();
            DROP INDEX IF EXISTS cocoindex.idx_dcm_code_embeddings_fts;
            ALTER TABLE cocoindex.dcm_code_embeddings
                DROP COLUMN IF EXISTS search_vector;
        """,
    )

    go_repos = [
        (control_plane_dir, "dcm-control-plane"),
        (cli_dir, "dcm-cli"),
        (kubevirt_sp_dir, "dcm-kubevirt-sp"),
        (k8s_container_sp_dir, "dcm-k8s-container-sp"),
        (acm_cluster_sp_dir, "dcm-acm-cluster-sp"),
        (three_tier_sp_dir, "dcm-three-tier-sp"),
        (utilities_dir, "dcm-utilities"),
    ]

    for repo_dir, repo_tag in go_repos:
        files = localfs.walk_dir(
            repo_dir,
            recursive=True,
            path_matcher=PatternFilePathMatcher(
                included_patterns=["**/*.go"],
                excluded_patterns=["**/vendor/**", "**/*_test.go", "**/zz_generated*"],
            ),
            live=True,
        )
        await coco.mount_each(
            coco.component_subpath(repo_tag),
            process_code_file, files.items(),
            table, repo_dir, repo_tag,
        )

    workflow_files = localfs.walk_dir(
        shared_workflows_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.sh", "**/*.yml"],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("dcm-shared-workflows"),
        process_code_file, workflow_files.items(),
        table, shared_workflows_dir, "dcm-shared-workflows",
    )


code_app = coco.App(
    "dcm-code", code_main,
    control_plane_dir=DCM_CONTROL_PLANE_DIR,
    cli_dir=DCM_CLI_DIR,
    kubevirt_sp_dir=DCM_KUBEVIRT_SP_DIR,
    k8s_container_sp_dir=DCM_K8S_CONTAINER_SP_DIR,
    acm_cluster_sp_dir=DCM_ACM_CLUSTER_SP_DIR,
    three_tier_sp_dir=DCM_THREE_TIER_SP_DIR,
    utilities_dir=DCM_UTILITIES_DIR,
    shared_workflows_dir=DCM_SHARED_WORKFLOWS_DIR,
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
        t = threading.Thread(target=_run_app, name=f"dcm-{name}", daemon=True)
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
        t = threading.Thread(target=_issues_poll_loop, name="dcm-issues", daemon=True)
        t.start()
        threads.append(t)

    log.info("All %d apps launched — docs/code watching files, issues polling every %ds",
             len(threads), ISSUES_POLL_INTERVAL)

    for t in threads:
        t.join()


def main():
    parser = argparse.ArgumentParser(
        description="CocoIndex flows for DCM — incremental ingestion"
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

    log.info("Starting DCM CocoIndex in %s mode — apps: %s", args.mode, ", ".join(sorted(selected)))
    log.info("  Architecture dir:    %s", DCM_ARCHITECTURE_DIR)
    log.info("  Docs dir:            %s", DCM_DOCS_DIR)
    log.info("  Enhancements dir:    %s", DCM_ENHANCEMENTS_DIR)
    log.info("  Control-plane dir:   %s", DCM_CONTROL_PLANE_DIR)
    log.info("  CLI dir:             %s", DCM_CLI_DIR)
    log.info("  KubeVirt SP dir:     %s", DCM_KUBEVIRT_SP_DIR)
    log.info("  K8s Container SP:    %s", DCM_K8S_CONTAINER_SP_DIR)
    log.info("  ACM Cluster SP:      %s", DCM_ACM_CLUSTER_SP_DIR)
    log.info("  Three-tier SP:       %s", DCM_THREE_TIER_SP_DIR)
    log.info("  Utilities dir:       %s", DCM_UTILITIES_DIR)
    log.info("  Shared workflows:    %s", DCM_SHARED_WORKFLOWS_DIR)
    log.info("  Issues repos:        %s", ", ".join(ISSUES_REPOS))
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
