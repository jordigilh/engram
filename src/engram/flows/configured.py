#!/usr/bin/env python3
"""Configuration-driven CocoIndex adapters for arbitrary deployments.

This is the only ingestion flow entrypoint.  A deployment-local TOML file
describes filesystem sources and tracker connectors; the Python package owns
only reusable ingestion algorithms.  Adding a project therefore does not add
an import, module, registry branch, or console script.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import re
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, AsyncIterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import cocoindex as coco
from cocoindex.connectors import localfs
from cocoindex.resources.file import PatternFilePathMatcher

from engram import chunking, contradiction_resolution, correction_gate
from engram.project_config import (
    DEFAULT_POOL_MAX_SIZE,
    DEFAULT_POOL_MIN_SIZE,
    ProjectAdapterConfig,
    ProjectIssueSource,
    ProjectSource,
    effective_source_tag,
    load_adapter_config,
    sql_identifier,
)
from engram.synthesis import synthesize_document

log = logging.getLogger("engram-configured-flow")

_CONFIG: ProjectAdapterConfig | None = None
_HINDSIGHT_URL = "http://localhost:8888"
_PG_DSN = "postgresql://hindsight:hindsight@localhost:5432/hindsight"
_COCOINDEX_DB = pathlib.Path("~/.engram/configured-cocoindex.db").expanduser()
_PG_POOL_MIN_SIZE = DEFAULT_POOL_MIN_SIZE
_PG_POOL_MAX_SIZE = DEFAULT_POOL_MAX_SIZE
_TRANSCRIPT_WATERMARKS_PATH = pathlib.Path("~/.engram/logs/cocoindex-transcript-watermarks.json").expanduser()
_TRANSCRIPT_WATERMARKS_LOCK = asyncio.Lock()

# One generic ContextKey is enough because this module is imported once per
# process.  Its name is intentionally not shared with historical modules.
PG_POOL: coco.ContextKey[Any] = coco.ContextKey("configured_project_pg_pool")

# Generic transcript gates.  These are intentionally project-neutral and are
# also the regex fallback used when ENGRAM_CORRECTION_DETECTOR=regex; keeping
# the fallback here preserves the historical transcript behavior without
# importing a project-specific flow module.
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


def configure(config: ProjectAdapterConfig) -> None:
    """Set process-local runtime values before constructing CocoIndex apps."""
    global _CONFIG, _HINDSIGHT_URL, _PG_DSN, _COCOINDEX_DB
    global _PG_POOL_MIN_SIZE, _PG_POOL_MAX_SIZE, _TRANSCRIPT_WATERMARKS_PATH
    _CONFIG = config
    _HINDSIGHT_URL = config.hindsight_url
    _PG_DSN = config.pg_dsn
    _COCOINDEX_DB = config.cocoindex_db
    _PG_POOL_MIN_SIZE = config.pg_pool_min_size
    _PG_POOL_MAX_SIZE = config.pg_pool_max_size
    _TRANSCRIPT_WATERMARKS_PATH = (
        config.transcript_watermarks
        or pathlib.Path("~/.engram/logs/cocoindex-transcript-watermarks.json").expanduser()
    )


def _wait_for_hindsight(max_retries: int = 30, delay: float = 2.0) -> None:
    for attempt in range(max_retries):
        try:
            request = Request(f"{_HINDSIGHT_URL}/health", method="GET")
            with urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError, OSError):
            wait = min(delay * (1.5 ** attempt), 30)
            log.warning("Hindsight not ready (attempt %d/%d), retrying in %.1fs", attempt + 1, max_retries, wait)
            time.sleep(wait)
    log.error("Hindsight not reachable after %d attempts; continuing", max_retries)


def hindsight_retain(
    bank_id: str,
    content: str,
    document_id: str,
    timestamp: str | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"content": content, "document_id": document_id}
    if timestamp:
        item["timestamp"] = timestamp
    if metadata:
        item["metadata"] = metadata
    if tags:
        item["tags"] = tags
    request = Request(
        f"{_HINDSIGHT_URL}/v1/default/banks/{bank_id}/memories",
        data=json.dumps({"items": [item]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            log.error("retain failed for %s/%s: %s", bank_id, document_id, exc)
    return {}


@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    from cocoindex.connectors import postgres

    builder.settings.db_path = _COCOINDEX_DB
    pool = await postgres.create_pool(
        _PG_DSN,
        min_size=_PG_POOL_MIN_SIZE,
        max_size=_PG_POOL_MAX_SIZE,
    )
    builder.provide(PG_POOL, pool)
    yield
    pool.close()


def _relative_path(file: localfs.File, base_dir: pathlib.Path) -> str:
    try:
        return str(file.file_path.resolve().relative_to(base_dir.resolve()))
    except ValueError:
        return str(file.file_path.path)


def _source_json(sources: tuple[ProjectSource, ...]) -> str:
    return json.dumps(
        [
            {
                "tag": effective_source_tag(source),
                "root": str(source.root),
                "docs_include": list(source.docs_include),
                "docs_exclude": list(source.docs_exclude),
                "code_include": list(source.code_include),
                "code_exclude": list(source.code_exclude),
                "language": source.language,
                "document_format": source.document_format,
                "branch": source.branch,
            }
            for source in sources
        ]
    )


def _sources(value: str) -> list[dict[str, Any]]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise ValueError("serialized project sources must be a list of tables")
    return decoded


def _document_base_id(source_tag: str, relative: str) -> str:
    path = pathlib.PurePosixPath(relative)
    suffix = path.suffix
    name = relative[:-len(suffix)] if suffix else relative
    return f"{source_tag}--{name.replace('/', '--')}"


def _file_timestamp(path: pathlib.Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return None


def _retain_document(
    *,
    bank_id: str,
    content: str,
    source_tag: str,
    relative: str,
    timestamp: str | None,
    chunk_size: int = 800,
    chunk_overlap: int = 200,
) -> None:
    base_id = _document_base_id(source_tag, relative)
    synthesis = synthesize_document(base_id, content)
    metadata = {
        "source": "cocoindex",
        "repo": source_tag,
        "key_sentences": " | ".join(synthesis["key_sentences"]),
        "keywords": ", ".join(synthesis["keywords"]),
    }
    section = pathlib.PurePosixPath(relative).parts[0] if "/" in relative else "root"
    for key, chunk in chunking.split_markdown_sections(
        content, chunk_size=chunk_size, chunk_overlap=chunk_overlap
    ):
        document_id = base_id if not key else f"{base_id}--{key}"
        hindsight_retain(
            bank_id=bank_id,
            content=chunk,
            document_id=document_id,
            timestamp=timestamp,
            metadata=metadata,
            tags=[section, source_tag],
        )


@coco.fn(memo=True)
async def process_doc_file(
    file: localfs.File,
    base_dir: pathlib.Path,
    bank_id: str,
    source_tag: str,
) -> None:
    content = await file.read_text()
    if not content.strip():
        return
    relative = _relative_path(file, base_dir)
    _retain_document(
        bank_id=bank_id,
        content=content,
        source_tag=source_tag,
        relative=relative,
        timestamp=_file_timestamp(file.file_path.resolve()),
    )


@coco.fn(memo=True)
async def process_pdf_file(
    file: localfs.File,
    base_dir: pathlib.Path,
    bank_id: str,
    source_tag: str,
) -> None:
    import pdfplumber

    path = file.file_path.resolve()
    with pdfplumber.open(str(path)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    content = "\n\n".join(
        f"--- Page {index + 1} ---\n\n{text}"
        for index, text in enumerate(pages)
        if text.strip()
    )
    if not content.strip():
        return
    _retain_document(
        bank_id=bank_id,
        content=content,
        source_tag=source_tag,
        relative=_relative_path(file, base_dir),
        timestamp=_file_timestamp(path),
    )


@coco.fn
async def docs_main(sources_json: str, bank_id: str, live: bool = True) -> None:
    for source in _sources(sources_json):
        includes = source.get("docs_include", [])
        if not includes:
            continue
        root = pathlib.Path(source["root"])
        if not root.is_dir():
            log.warning("Skipping documentation source %s: root does not exist: %s", source["tag"], root)
            continue
        files = localfs.walk_dir(
            root,
            recursive=True,
            path_matcher=PatternFilePathMatcher(
                included_patterns=includes,
                excluded_patterns=source.get("docs_exclude", []),
            ),
            live=live,
        )
        processor = process_pdf_file if source.get("document_format") == "pdf" else process_doc_file
        await coco.mount_each(
            coco.component_subpath(f"docs_{source['tag']}"),
            processor,
            files.items(),
            root,
            bank_id,
            source["tag"],
        )


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
    table: Any,
    base_dir: pathlib.Path,
    repo_tag: str,
) -> None:
    content = await file.read_text()
    if not content.strip():
        return
    relative = _relative_path(file, base_dir)
    filepath = f"{repo_tag}/{relative}"
    chunks = chunking.split_code(content, filename=filepath, chunk_size=1000, chunk_overlap=300)
    embeddings = await chunking.embed_code_chunks(chunks)
    for index, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        table.declare_row(
            row=CodeEmbedding(
                id=f"{filepath}:{index}",
                filepath=filepath,
                chunk_index=index,
                code=chunk,
                embedding=embedding,
                search_text=f"{filepath} {chunk}",
            )
        )


@coco.fn
async def code_main(sources_json: str, code_table: str, live: bool = True) -> None:
    from cocoindex.connectors import postgres

    code_table = sql_identifier(code_table, "code table")
    embedding_dim = await chunking.code_embedding_dim()
    schema = await postgres.TableSchema.from_class(
        CodeEmbedding,
        primary_key=["id"],
        column_overrides={
            "embedding": postgres.PgType(
                f"vector({embedding_dim})",
                encoder=lambda value: "[" + ",".join(str(item) for item in value) + "]",
            ),
        },
    )
    table = await postgres.mount_table_target(PG_POOL, code_table, schema, pg_schema_name="cocoindex")
    table.declare_vector_index(column="embedding", metric="cosine")
    function_name = f"update_{code_table}_search_vector"
    trigger_name = f"trg_{code_table}_search_vector"
    index_name = f"idx_{code_table}_fts"
    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql=f"""
            ALTER TABLE cocoindex.{code_table}
                ADD COLUMN IF NOT EXISTS search_vector tsvector;
            CREATE INDEX IF NOT EXISTS {index_name}
                ON cocoindex.{code_table} USING gin(search_vector);
            CREATE OR REPLACE FUNCTION cocoindex.{function_name}()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS {trigger_name} ON cocoindex.{code_table};
            CREATE TRIGGER {trigger_name}
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.{code_table}
                FOR EACH ROW EXECUTE FUNCTION cocoindex.{function_name}();
            UPDATE cocoindex.{code_table}
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, code, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql=f"""
            DROP TRIGGER IF EXISTS {trigger_name} ON cocoindex.{code_table};
            DROP FUNCTION IF EXISTS cocoindex.{function_name}();
            DROP INDEX IF EXISTS cocoindex.{index_name};
            ALTER TABLE cocoindex.{code_table} DROP COLUMN IF EXISTS search_vector;
        """,
    )
    for source in _sources(sources_json):
        includes = source.get("code_include", [])
        if not includes:
            continue
        root = pathlib.Path(source["root"])
        if not root.is_dir():
            log.warning("Skipping code source %s: root does not exist: %s", source["tag"], root)
            continue
        files = localfs.walk_dir(
            root,
            recursive=True,
            path_matcher=PatternFilePathMatcher(
                included_patterns=includes,
                excluded_patterns=source.get("code_exclude", []),
            ),
            live=live,
        )
        await coco.mount_each(
            coco.component_subpath(f"code_{source['tag']}"),
            process_code_file,
            files.items(),
            table,
            root,
            source["tag"],
        )


# ---------------------------------------------------------------------------
# Generic tracker connectors
# ---------------------------------------------------------------------------

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}


def _fetch_github_items(provider: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    fields = "number,title,body,state,labels,milestone,createdAt,updatedAt,comments,author"
    result_items: list[tuple[dict[str, Any], str]] = []
    for repo in provider.get("repos", []):
        for kind in provider.get("item_kinds", ["issue", "pr"]):
            command = [
                "gh",
                kind,
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--limit",
                str(provider.get("limit", 10000)),
                "--json",
                fields,
            ]
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
                if completed.returncode:
                    log.error("gh %s list failed for %s: %s", kind, repo, completed.stderr[:300])
                    continue
                batch = json.loads(completed.stdout)
                if not isinstance(batch, list):
                    continue
                for item in batch:
                    if isinstance(item, dict):
                        item["_kind"] = kind
                        result_items.append((item, repo))
                log.info("Fetched %d %ss from %s", len(batch), kind, repo)
            except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                log.error("fetching %s from %s failed: %s", kind, repo, exc)
    return result_items


def _jira_token(account: str, service: str) -> str | None:
    token = os.environ.get("JIRA_API_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.error("Jira Keychain lookup failed: %s", exc)
        return None
    token = result.stdout.strip()
    if result.returncode or not token:
        log.error("Jira Keychain item %s/%s was not available", account, service)
        return None
    return token


def _jql_from_provider(provider: dict[str, Any]) -> str | None:
    jql = provider.get("jql")
    if isinstance(jql, str) and jql.strip():
        return jql.strip()
    raw_path = provider.get("jql_file")
    if not raw_path:
        return None
    try:
        keys = [
            line.strip()
            for line in pathlib.Path(raw_path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except OSError as exc:
        log.error("Could not read Jira query file %s: %s", raw_path, exc)
        return None
    if not keys:
        return None
    template = str(provider.get("jql_template", "key in ({keys}) order by updated desc"))
    return template.format(keys=", ".join(keys))


def _fetch_jira_items(provider: dict[str, Any]) -> list[dict[str, Any]]:
    token = _jira_token(provider["keychain_account"], provider["keychain_service"])
    jql = _jql_from_provider(provider)
    if not token or not jql:
        return []
    auth = base64.b64encode(f"{provider['email']}:{token}".encode()).decode()
    fields = [
        "summary",
        "description",
        "status",
        "issuetype",
        "priority",
        "labels",
        "reporter",
        "created",
        "updated",
        "comment",
        "parent",
    ]
    limit = int(provider.get("limit", 10000))
    endpoint = provider["server"].rstrip("/") + "/rest/api/3/search/jql"
    items: list[dict[str, Any]] = []
    next_page_token: str | None = None
    while len(items) < limit:
        payload: dict[str, Any] = {
            "jql": jql,
            "maxResults": min(100, limit - len(items)),
            "fields": fields,
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        request = Request(
            endpoint,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {auth}",
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                document = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            log.error("Jira request failed: %s", exc)
            break
        batch = document.get("issues", [])
        if not isinstance(batch, list) or not batch:
            break
        items.extend(item for item in batch if isinstance(item, dict))
        if document.get("isLast", True):
            break
        next_page_token = document.get("nextPageToken")
        if not isinstance(next_page_token, str) or not next_page_token:
            break
    log.info("Fetched %d Jira issues", len(items[:limit]))
    return items[:limit]


def _repo_name(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def _human_comments(issue: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        comment
        for comment in issue.get("comments", []) or []
        if comment.get("authorAssociation", "NONE") in TRUSTED_ASSOCIATIONS
        and not comment.get("author", {}).get("login", "").endswith("[bot]")
        and len(comment.get("body", "")) > 20
    ][:10]


def _format_comment(comment: dict[str, Any]) -> str:
    author = comment.get("author", {}).get("login", "?")
    body = str(comment.get("body", "")).strip()
    if len(body) > 2000:
        body = body[:2000] + "\n[...]"
    return f"**@{author}:**\n{body}"


def _format_issue_header(issue: dict[str, Any], repo: str) -> str:
    kind = issue.get("_kind", "issue")
    label = "PR" if kind == "pr" else "Issue"
    author = issue.get("author", {}).get("login", "unknown")
    created = str(issue.get("createdAt", ""))[:10]
    parts = [
        f"# {label} #{issue.get('number', '?')} ({_repo_name(repo)}): {issue.get('title', '')}",
        f"Repo: {repo} | Author: {author} | Created: {created}",
        "",
    ]
    body = issue.get("body", "") or ""
    if body.strip():
        parts.append(body.strip())
    return "\n".join(parts)


@coco.fn(memo=True)
def process_issue(
    issue: dict[str, Any],
    repo: str,
    bank_id: str,
    chunk_size: int = 1200,
    chunk_overlap: int = 300,
    document_prefix: str | None = None,
) -> None:
    header = _format_issue_header(issue, repo)
    if len(header.strip()) < 50:
        return
    kind = issue.get("_kind", "issue")
    number = issue.get("number", "?")
    state = str(issue.get("state", "OPEN")).lower()
    short_repo = _repo_name(repo)
    labels = [item.get("name", "") for item in issue.get("labels", []) if isinstance(item, dict)]
    milestone = issue.get("milestone") or {}
    comments = [_format_comment(item) for item in _human_comments(issue)]
    prefix = document_prefix or ""
    base_id = f"{prefix}-" if prefix else ""
    base_id += f"{short_repo}-{kind}-{number}"
    tags = [state, kind, short_repo] + labels[:5]
    metadata: dict[str, str] = {
        "source": "cocoindex",
        "repo": repo,
        "kind": kind,
        "number": str(number),
        "state": state,
    }
    if isinstance(milestone, dict) and milestone.get("title"):
        tags.append(f"milestone:{milestone['title']}")
        metadata["milestone"] = str(milestone["title"])
        metadata["milestone_due"] = str(milestone.get("dueOn", "") or "")
    for suffix, chunk in chunking.split_issue_sections(
        header, comments, chunk_size=chunk_size, chunk_overlap=chunk_overlap
    ):
        hindsight_retain(
            bank_id=bank_id,
            content=chunk,
            document_id=base_id if not suffix else f"{base_id}-{suffix}",
            timestamp=issue.get("updatedAt", ""),
            metadata=metadata,
            tags=tags,
        )


def _adf_to_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(item) for item in node)
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type", "")
    if node_type == "text":
        return str(node.get("text", ""))
    if node_type == "mention":
        return str(node.get("attrs", {}).get("text", ""))
    if node_type == "hardBreak":
        return "\n"
    if node_type == "rule":
        return "\n---\n"
    inner = "".join(_adf_to_text(item) for item in node.get("content", []))
    return inner + ("\n" if node_type in {"paragraph", "heading", "listItem", "codeBlock"} else "")


def _jira_header(issue: dict[str, Any], provider: dict[str, Any]) -> str:
    fields = issue.get("fields", {}) or {}
    issue_type = (fields.get("issuetype") or {}).get("name", "Issue")
    reporter = (fields.get("reporter") or {}).get("displayName", "unknown")
    created = str(fields.get("created", ""))[:10]
    parent = (fields.get("parent") or {}).get("key", "")
    line = f"Project: {provider.get('project', 'Jira')} | Reporter: {reporter} | Created: {created}"
    if parent:
        line += f" | Parent: {parent}"
    description = _adf_to_text(fields.get("description")).strip()
    return "\n".join(
        [
            f"# {issue_type} {issue.get('key', '?')}: {fields.get('summary', '')}",
            line,
            "",
            description,
        ]
    ).rstrip()


@coco.fn(memo=True)
def process_jira_issue(
    issue: dict[str, Any],
    bank_id: str,
    chunk_size: int = 1200,
    chunk_overlap: int = 300,
    document_prefix: str | None = None,
    project_name: str = "Jira",
) -> None:
    provider = {"project": project_name}
    header = _jira_header(issue, provider)
    if len(header.strip()) < 50:
        return
    fields = issue.get("fields", {}) or {}
    key = str(issue.get("key", "unknown"))
    status = str((fields.get("status") or {}).get("name", "unknown")).lower()
    issue_type = str((fields.get("issuetype") or {}).get("name", "issue")).lower()
    labels = [str(item) for item in fields.get("labels", []) or []]
    comments = []
    for comment in ((fields.get("comment") or {}).get("comments", []) or [])[:10]:
        text = _adf_to_text(comment.get("body")).strip()
        if len(text) > 20:
            if len(text) > 2000:
                text = text[:2000] + "\n[...]"
            comments.append(f"**{(comment.get('author') or {}).get('displayName', '?')}:**\n{text}")
    prefix = document_prefix or "jira"
    base_id = f"{prefix}-{key}"
    tags = [status, issue_type, "jira"] + labels[:5]
    parent = (fields.get("parent") or {}).get("key")
    if parent:
        tags.append(str(parent))
    metadata = {
        "source": "cocoindex",
        "tracker": "jira",
        "key": key,
        "status": status,
        "project": project_name,
    }
    for suffix, chunk in chunking.split_issue_sections(
        header, comments, chunk_size=chunk_size, chunk_overlap=chunk_overlap
    ):
        hindsight_retain(
            bank_id=bank_id,
            content=chunk,
            document_id=base_id if not suffix else f"{base_id}-{suffix}",
            timestamp=fields.get("updated", ""),
            metadata=metadata,
            tags=tags,
        )


def _github_graphql(query: str, **variables: Any) -> dict[str, Any]:
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        command.extend(["-F", f"{key}={value}"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode:
            log.error("gh api graphql failed: %s", result.stderr[:300])
            return {}
        decoded = json.loads(result.stdout)
        return decoded if isinstance(decoded, dict) else {}
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        log.error("GitHub GraphQL request failed: %s", exc)
        return {}


_DISCUSSIONS_QUERY = """
query($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    discussions(first: $first, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { number title body url category { name } author { login }
        createdAt updatedAt comments(first: 20) {
          nodes { author { login } body authorAssociation }
        }
      }
    }
  }
}
"""


def _fetch_discussions(provider: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    result: list[tuple[dict[str, Any], str]] = []
    for repo in provider.get("repos", []):
        if "/" not in repo:
            continue
        owner, name = repo.split("/", 1)
        data = _github_graphql(_DISCUSSIONS_QUERY, owner=owner, name=name, first=min(int(provider.get("limit", 50)), 100))
        nodes = (((data.get("data") or {}).get("repository") or {}).get("discussions") or {}).get("nodes", [])
        for item in nodes:
            if isinstance(item, dict):
                item["_kind"] = "discussion"
                result.append((item, repo))
    return result


def _discussion_comments(item: dict[str, Any]) -> list[str]:
    comments = (item.get("comments") or {}).get("nodes", []) or []
    result = []
    for comment in comments[:10]:
        body = str(comment.get("body", "")).strip()
        login = (comment.get("author") or {}).get("login", "?")
        if len(body) > 20 and not login.endswith("[bot]"):
            result.append(_format_comment({"author": {"login": login}, "body": body}))
    return result


@coco.fn(memo=True)
def process_discussion(
    item: dict[str, Any],
    repo: str,
    bank_id: str,
    chunk_size: int = 1200,
    chunk_overlap: int = 300,
    document_prefix: str | None = None,
) -> None:
    category = (item.get("category") or {}).get("name", "general")
    author = (item.get("author") or {}).get("login", "unknown")
    created = str(item.get("createdAt", ""))[:10]
    header = "\n".join(
        [
            f"# Discussion #{item.get('number', '?')} ({_repo_name(repo)}): {item.get('title', '')}",
            f"Repo: {repo} | Category: {category} | Author: {author} | Created: {created}",
            "",
            str(item.get("body", "") or "").strip(),
        ]
    ).rstrip()
    if len(header.strip()) < 50:
        return
    prefix = document_prefix or ""
    base = f"{prefix}-" if prefix else ""
    base += f"{_repo_name(repo)}-discussion-{item.get('number', '?')}"
    tags = ["discussion", _repo_name(repo), category.lower().replace(" ", "-")]
    metadata = {
        "source": "cocoindex",
        "repo": repo,
        "kind": "discussion",
        "number": str(item.get("number", "?")),
        "category": category,
    }
    for suffix, chunk in chunking.split_issue_sections(
        header, _discussion_comments(item), chunk_size=chunk_size, chunk_overlap=chunk_overlap
    ):
        hindsight_retain(
            bank_id=bank_id,
            content=chunk,
            document_id=base if not suffix else f"{base}-{suffix}",
            timestamp=item.get("updatedAt", ""),
            metadata=metadata,
            tags=tags,
        )


_PROJECTS_QUERY = """
query($org: String!, $number: Int!, $after: String) {
  organization(login: $org) { projectV2(number: $number) {
    title shortDescription items(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { content {
        ... on Issue { number title url state repository { name } }
        ... on PullRequest { number title url state repository { name } }
      } fieldValues(first: 10) { nodes {
        ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
      } }
    }
  } }
}
"""


def _fetch_project_items(organization: str, number: int) -> tuple[str, str, list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    title = ""
    description = ""
    after: str | None = None
    for _ in range(10):
        variables: dict[str, Any] = {"org": organization, "number": number}
        if after:
            variables["after"] = after
        data = _github_graphql(_PROJECTS_QUERY, **variables)
        project = (((data.get("data") or {}).get("organization") or {}).get("projectV2"))
        if not project:
            break
        title = project.get("title", title)
        description = project.get("shortDescription", description) or description
        block = project.get("items", {})
        items.extend(item for item in block.get("nodes", []) if isinstance(item, dict))
        page_info = block.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return title, description, items


def _board_status(item: dict[str, Any]) -> str:
    for field_value in (item.get("fieldValues") or {}).get("nodes", []):
        if (field_value.get("field") or {}).get("name") == "Status":
            return field_value.get("name", "")
    return "(no status)"


@coco.fn(memo=True)
def process_board(
    number: int,
    organization: str,
    bank_id: str,
    document_prefix: str | None = None,
) -> None:
    title, description, items = _fetch_project_items(organization, number)
    if not title:
        return
    grouped: dict[str, list[str]] = {}
    for item in items:
        content = item.get("content") or {}
        if not content:
            continue
        repo = (content.get("repository") or {}).get("name", "")
        grouped.setdefault(_board_status(item), []).append(
            f"- #{content.get('number', '?')} ({repo}): {content.get('title', '')}"
        )
    order = ["In Progress", "Next", "Review", "Backlog", "Epics", "Done", "(no status)"]
    statuses = [status for status in order if status in grouped] + [status for status in grouped if status not in order]
    parts = [f"# Project Board: {title} (#{number})"]
    if description:
        parts.append(description)
    parts.extend([f"Total items: {len(items)}", ""])
    for status in statuses:
        parts.extend([f"## Status: {status} ({len(grouped[status])})", *grouped[status], ""])
    slug = title.lower().replace(" ", "-").replace("/", "-")
    prefix = document_prefix or "board"
    hindsight_retain(
        bank_id=bank_id,
        content="\n".join(parts),
        document_id=f"{prefix}-{number}-{slug}",
        timestamp=datetime.now().isoformat(),
        metadata={"source": "cocoindex", "kind": "board", "board_number": str(number), "board_title": title},
        tags=["board", "roadmap", slug],
    )


def _issue_provider_json(source: ProjectIssueSource) -> dict[str, Any]:
    return {
        "provider": source.provider,
        "repos": list(source.repos),
        "item_kinds": list(source.item_kinds),
        "limit": source.limit,
        "server": source.jira_server,
        "email": source.jira_email,
        "jql": source.jira_jql,
        "jql_file": str(source.jira_jql_file) if source.jira_jql_file else None,
        "jql_template": source.jira_jql_template,
        "keychain_account": source.jira_keychain_account,
        "keychain_service": source.jira_keychain_service,
        "github_organization": source.github_organization,
        "github_project_numbers": list(source.github_project_numbers),
        "project": source.tracker_project,
        "chunk_size": source.chunk_size,
        "chunk_overlap": source.chunk_overlap,
        "document_prefix": source.document_prefix,
    }


@coco.fn
def issues_main(providers_json: str, bank_id: str) -> None:
    providers = json.loads(providers_json)
    if not isinstance(providers, list):
        raise ValueError("serialized issue providers must be a list")
    for provider in providers:
        kind = provider["provider"]
        if kind == "github":
            for issue, repo in _fetch_github_items(provider):
                process_issue(
                    issue,
                    repo,
                    bank_id,
                    int(provider.get("chunk_size", 1200)),
                    int(provider.get("chunk_overlap", 300)),
                    provider.get("document_prefix"),
                )
        elif kind == "jira":
            project_name = str(provider.get("project", "Jira"))
            for issue in _fetch_jira_items(provider):
                process_jira_issue(
                    issue,
                    bank_id,
                    int(provider.get("chunk_size", 1200)),
                    int(provider.get("chunk_overlap", 300)),
                    provider.get("document_prefix"),
                    project_name,
                )
        elif kind == "github_discussions":
            for discussion, repo in _fetch_discussions(provider):
                process_discussion(
                    discussion,
                    repo,
                    bank_id,
                    int(provider.get("chunk_size", 1200)),
                    int(provider.get("chunk_overlap", 300)),
                    provider.get("document_prefix"),
                )
        elif kind == "github_projects":
            for number in provider.get("github_project_numbers", []):
                process_board(number, provider["github_organization"], bank_id, provider.get("document_prefix"))


# ---------------------------------------------------------------------------
# Optional transcript connector
# ---------------------------------------------------------------------------

def _load_transcript_watermarks() -> dict[str, Any]:
    if _TRANSCRIPT_WATERMARKS_PATH.exists():
        try:
            return json.loads(_TRANSCRIPT_WATERMARKS_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("Corrupt transcript watermark file %s; starting fresh", _TRANSCRIPT_WATERMARKS_PATH)
    return {}


def _save_transcript_watermarks(value: dict[str, Any]) -> None:
    _TRANSCRIPT_WATERMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _TRANSCRIPT_WATERMARKS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(_TRANSCRIPT_WATERMARKS_PATH)


def _is_correction(text: str) -> bool:
    return correction_gate.is_correction(text, CORRECTION_PATTERNS)


def _is_instruction(text: str) -> bool:
    if not text or len(text) < 20 or len(text) > 2000:
        return False
    return any(pattern.search(text) for pattern in INSTRUCTION_PATTERNS)


def _transcript_user_text(message: dict[str, Any]) -> str:
    content = message.get("message", {}).get("content", [])
    if isinstance(content, str):
        raw = content
    else:
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = str(block.get("text", ""))
            if text.startswith("<external_links>"):
                continue
            match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
            texts.append(match.group(1) if match else text)
        raw = "\n".join(texts)
    return raw.strip() if not correction_gate.is_system_boilerplate(raw) else ""


def _transcript_assistant_text(message: dict[str, Any]) -> str:
    content = message.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _learning_windows(messages: list[dict[str, Any]], start_index: int = 0, window: int = 2) -> list[str]:
    parsed: list[dict[str, Any]] = []
    for raw_index, message in enumerate(messages):
        role = message.get("role", "")
        if role == "user":
            text = _transcript_user_text(message)
            if text:
                parsed.append({"role": role, "text": text, "correction": _is_correction(text), "instruction": _is_instruction(text), "raw_index": raw_index})
        elif role == "assistant":
            text = _transcript_assistant_text(message)
            if text:
                parsed.append({"role": role, "text": text[:400], "correction": False, "instruction": False, "raw_index": raw_index})
    signals = [
        index
        for index, item in enumerate(parsed)
        if (item["correction"] or item["instruction"]) and item["raw_index"] >= start_index
    ]
    windows: list[str] = []
    used: set[int] = set()
    for signal in signals:
        if signal in used:
            continue
        used.add(signal)
        lines = []
        for index in range(max(0, signal - window), min(len(parsed), signal + window + 1)):
            item = parsed[index]
            marker = ""
            if index == signal:
                marker = "[CORRECTION] " if item["correction"] else "[INSTRUCTION] "
            lines.append(f"{marker}{item['role'].title()}: {item['text'][:300]}")
        windows.append("\n\n".join(lines))
    return windows


def _window_document_id(transcript_id: str, window_text: str) -> str:
    digest = hashlib.sha1(window_text.encode("utf-8")).hexdigest()[:16]
    return f"transcript-{transcript_id}-w{digest}"


@coco.fn(memo=True)
async def process_transcript(
    file: localfs.File,
    bank_id: str,
    transcripts_dir: pathlib.Path,
    workspace_prefixes_json: str,
    project_name: str,
) -> None:
    content = await file.read_text()
    if not content.strip():
        return
    messages = []
    for line in content.splitlines():
        try:
            if line.strip():
                messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not messages:
        return
    path = file.file_path.resolve()
    try:
        relative_parts = path.relative_to(transcripts_dir.resolve()).parts
    except ValueError:
        log.warning("Skipping transcript outside configured root: %s", path)
        return
    if not relative_parts:
        return
    project_dir = relative_parts[0]
    prefixes = json.loads(workspace_prefixes_json)
    if not any(project_dir.startswith(prefix) for prefix in prefixes):
        return
    project = project_name
    relative_path = getattr(file.file_path, "path", file.file_path)
    transcript_id = pathlib.Path(relative_path).stem
    async with _TRANSCRIPT_WATERMARKS_LOCK:
        previous = _load_transcript_watermarks().get(transcript_id, {}).get("message_count", 0)
    start_index = previous if previous <= len(messages) else 0
    for window_text in _learning_windows(messages, start_index=start_index):
        if not window_text.strip():
            continue
        tags = [project] if project else []
        if "[CORRECTION]" in window_text:
            resolution = contradiction_resolution.resolve(bank_id, window_text, project=project)
            if resolution.action == "queued":
                continue
            if resolution.action == "auto_resolved":
                tags.extend(["CORRECTION", "supersedes-prior-memory"])
        hindsight_retain(
            bank_id=bank_id,
            content=window_text,
            document_id=_window_document_id(transcript_id, window_text),
            metadata={"source": "cocoindex-transcript", "transcript_id": transcript_id},
            tags=tags or None,
        )
    if len(messages) > previous:
        async with _TRANSCRIPT_WATERMARKS_LOCK:
            watermarks = _load_transcript_watermarks()
            watermarks[transcript_id] = {"message_count": len(messages), "last_processed": datetime.now().isoformat()}
            _save_transcript_watermarks(watermarks)


@coco.fn
async def transcript_main(
    transcripts_dir: pathlib.Path,
    bank_id: str,
    workspace_prefixes_json: str,
    project_name: str,
    live: bool = True,
) -> None:
    prefixes = json.loads(workspace_prefixes_json)
    if not transcripts_dir.is_dir():
        log.warning("Skipping transcripts: root does not exist: %s", transcripts_dir)
        return
    files = localfs.walk_dir(
        transcripts_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=[f"{prefix}*/agent-transcripts/**/*.jsonl" for prefix in prefixes]
        ),
        live=live,
    )
    await coco.mount_each(
        coco.component_subpath("transcripts"),
        process_transcript,
        files.items(),
        bank_id,
        transcripts_dir,
        workspace_prefixes_json,
        project_name,
    )


def _git_pull_one(root: pathlib.Path) -> None:
    if not (root / ".git").exists():
        return
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only", "--quiet"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            log.warning("git pull failed for %s: %s", root, result.stderr[:300])
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.error("git pull failed for %s: %s", root, exc)


def build_apps(config: ProjectAdapterConfig) -> dict[str, coco.App]:
    """Build the selected deployment's apps from validated TOML values."""
    configure(config)
    apps: dict[str, coco.App] = {}
    if config.docs_bank and config.docs_sources:
        apps["docs"] = coco.App(
            f"{config.project}-docs",
            docs_main,
            sources_json=_source_json(config.docs_sources),
            bank_id=config.docs_bank,
            live=config.docs_live,
        )
    if config.code_table and config.code_sources:
        apps["code"] = coco.App(
            f"{config.project}-code",
            code_main,
            sources_json=_source_json(config.code_sources),
            code_table=config.code_table,
            live=config.code_live,
        )
    if config.issues_bank and config.issue_sources:
        apps["issues"] = coco.App(
            f"{config.project}-issues",
            issues_main,
            providers_json=json.dumps([_issue_provider_json(source) for source in config.issue_sources]),
            bank_id=config.issues_bank,
        )
    if config.transcript_enabled and config.transcript_bank and config.transcript_dir:
        apps["transcripts"] = coco.App(
            f"{config.project}-transcripts",
            transcript_main,
            transcripts_dir=config.transcript_dir,
            bank_id=config.transcript_bank,
            workspace_prefixes_json=json.dumps(list(config.workspace_prefixes)),
            project_name=config.project,
            live=config.docs_live,
        )
    return apps


def _run_repeating(name: str, app: coco.App, interval: int, threads: list[threading.Thread]) -> None:
    def poll() -> None:
        while True:
            try:
                app.update_blocking()
            except Exception:
                log.exception("%s app failed", name)
            time.sleep(interval)

    thread = threading.Thread(target=poll, name=f"engram-{name}", daemon=True)
    thread.start()
    threads.append(thread)


def _run_live(config: ProjectAdapterConfig, apps: dict[str, coco.App], selected: set[str]) -> None:
    threads: list[threading.Thread] = []
    for name in ("docs", "transcripts"):
        if name not in selected or name not in apps:
            continue
        thread = threading.Thread(
            target=lambda app=apps[name], app_name=name: app.update_blocking(
                live=config.docs_live if app_name == "docs" else config.docs_live
            ),
            name=f"engram-{config.project}-{name}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    if "code" in selected and "code" in apps:
        if config.code_live:
            thread = threading.Thread(
                target=lambda: apps["code"].update_blocking(live=True),
                name=f"engram-{config.project}-code",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        else:
            _run_repeating("code", apps["code"], config.code_poll_seconds, threads)
    if "issues" in selected and "issues" in apps:
        _run_repeating("issues", apps["issues"], config.issues_poll_seconds, threads)
    if "git-sync" in selected and config.git_sync:
        roots = tuple(dict.fromkeys(source.root for source in config.sources))

        def sync() -> None:
            while True:
                for root in roots:
                    _git_pull_one(root)
                time.sleep(config.git_sync_seconds)

        thread = threading.Thread(target=sync, name=f"engram-{config.project}-git-sync", daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()


def _default_selected_apps(apps: dict[str, coco.App]) -> set[str]:
    """Return unattended apps, keeping transcript retention opt-in."""
    return set(apps) - {"transcripts"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Configuration-driven Engram project flow")
    parser.add_argument("--project", required=True, help="Project key in projects.toml")
    parser.add_argument("--config", help="Path to projects.toml (defaults to ENGRAM_PROJECTS_CONFIG)")
    parser.add_argument("--mode", choices=["backfill", "live"], default="live")
    parser.add_argument("--apps", nargs="*", choices=["docs", "issues", "code", "transcripts", "git-sync"], default=None)
    args = parser.parse_args()

    config = load_adapter_config(args.project, args.config)
    apps = build_apps(config)
    # Transcript ingestion is an explicit opt-in.  Unlike docs/code/issues,
    # it retains raw chat text into shared memory and is intentionally kept
    # out of unattended services under the deployment cost policy.  Callers
    # can still request it with ``--apps transcripts``.
    selected = set(args.apps) if args.apps is not None else _default_selected_apps(apps)
    if config.git_sync and args.apps is None:
        selected.add("git-sync")
    unknown = selected - apps.keys() - ({"git-sync"} if config.git_sync else set())
    if unknown:
        parser.error(f"project {config.project!r} has no configured app(s): {', '.join(sorted(unknown))}")
    if not selected or (selected == {"git-sync"} and not config.git_sync):
        parser.error(f"project {config.project!r} has no runnable apps")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    _wait_for_hindsight()
    if args.mode == "backfill":
        if "git-sync" in selected:
            for root in dict.fromkeys(source.root for source in config.sources):
                _git_pull_one(root)
        for name in ("docs", "issues", "code", "transcripts"):
            if name in selected:
                apps[name].update_blocking(report_to_stdout=True)
    else:
        _run_live(config, apps, selected)


if __name__ == "__main__":
    main()
