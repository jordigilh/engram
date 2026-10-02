#!/usr/bin/env python3
"""Configuration-driven CocoIndex adapters for arbitrary projects.

One generic flow process is used for every project.  The project name selects a
table in ``~/.engram/projects.toml``; no project-specific Python module or
console-script entrypoint is needed.  Existing specialized flows remain
available for historical deployments, but new projects should use this
entrypoint.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import logging
import os
import pathlib
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

from engram import chunking
from engram.synthesis import synthesize_document
from engram.project_config import (
    DEFAULT_POOL_MAX_SIZE,
    DEFAULT_POOL_MIN_SIZE,
    ProjectAdapterConfig,
    ProjectSource,
    load_adapter_config,
    sql_identifier,
)

log = logging.getLogger("engram-configured-flow")

_CONFIG: ProjectAdapterConfig | None = None
_HINDSIGHT_URL = "http://localhost:8888"
_PG_DSN = "postgresql://hindsight:hindsight@localhost:5432/hindsight"
_COCOINDEX_DB = pathlib.Path("~/.engram/configured-cocoindex.db").expanduser()
_PG_POOL_MIN_SIZE = DEFAULT_POOL_MIN_SIZE
_PG_POOL_MAX_SIZE = DEFAULT_POOL_MAX_SIZE

# One module is imported once per process, so a single generic ContextKey is
# enough.  The key is intentionally distinct from all legacy per-project flow
# modules, which may still be imported by the test suite.
PG_POOL: coco.ContextKey[Any] = coco.ContextKey("configured_project_pg_pool")


def configure(config: ProjectAdapterConfig) -> None:
    """Set process-local runtime values before constructing CocoIndex apps."""
    global _CONFIG, _HINDSIGHT_URL, _PG_DSN, _COCOINDEX_DB
    global _PG_POOL_MIN_SIZE, _PG_POOL_MAX_SIZE
    _CONFIG = config
    _HINDSIGHT_URL = config.hindsight_url
    _PG_DSN = config.pg_dsn
    _COCOINDEX_DB = config.cocoindex_db
    _PG_POOL_MIN_SIZE = config.pg_pool_min_size
    _PG_POOL_MAX_SIZE = config.pg_pool_max_size


def _require_config() -> ProjectAdapterConfig:
    if _CONFIG is None:
        raise RuntimeError("configured flow has not been configured")
    return _CONFIG


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


def _relative_path(file: localfs.File, base_dir: pathlib.Path) -> str:
    try:
        return str(file.file_path.resolve().relative_to(base_dir.resolve()))
    except ValueError:
        return file.file_path.name


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
    try:
        timestamp = datetime.fromtimestamp(file.file_path.stat().st_mtime).isoformat()
    except OSError:
        timestamp = None
    base_id = f"{source_tag}--{relative.replace('/', '--').replace('.md', '')}"
    synthesis = synthesize_document(base_id, content)
    metadata = {
        "source": "cocoindex",
        "repo": source_tag,
        "key_sentences": " | ".join(synthesis["key_sentences"]),
        "keywords": ", ".join(synthesis["keywords"]),
    }
    for section, chunk in chunking.split_markdown_sections(content, chunk_size=800, chunk_overlap=200):
        document_id = base_id if not section else f"{base_id}--{section}"
        hindsight_retain(
            bank_id=bank_id,
            content=chunk,
            document_id=document_id,
            timestamp=timestamp,
            metadata=metadata,
            tags=[source_tag],
        )


def _source_json(sources: tuple[ProjectSource, ...]) -> str:
    return json.dumps([
        {
            "tag": source.tag,
            "root": str(source.root),
            "docs_include": list(source.docs_include),
            "docs_exclude": list(source.docs_exclude),
            "code_include": list(source.code_include),
            "code_exclude": list(source.code_exclude),
            "language": source.language,
        }
        for source in sources
    ])


def _sources(value: str) -> list[dict[str, Any]]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise ValueError("serialized project sources must be a list of tables")
    return decoded


@coco.fn
async def docs_main(sources_json: str, bank_id: str) -> None:
    for source in _sources(sources_json):
        includes = source.get("docs_include", [])
        if not includes:
            continue
        root = pathlib.Path(source["root"])
        files = localfs.walk_dir(
            root,
            recursive=True,
            path_matcher=PatternFilePathMatcher(
                included_patterns=includes,
                excluded_patterns=source.get("docs_exclude", []),
            ),
            live=True,
        )
        await coco.mount_each(
            coco.component_subpath(f"docs_{source['tag']}"),
            process_doc_file,
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
        row = CodeEmbedding(
            id=f"{filepath}:{index}",
            filepath=filepath,
            chunk_index=index,
            code=chunk,
            embedding=embedding,
            search_text=f"{filepath} {chunk}",
        )
        table.declare_row(row=row)


@coco.fn
async def code_main(sources_json: str, code_table: str) -> None:
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
    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql=f"""
            ALTER TABLE cocoindex.{code_table}
                ADD COLUMN IF NOT EXISTS search_vector tsvector;
            CREATE INDEX IF NOT EXISTS idx_{code_table}_fts
                ON cocoindex.{code_table} USING gin(search_vector);
            CREATE OR REPLACE FUNCTION cocoindex.update_{code_table}_search_vector()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS trg_{code_table}_search_vector ON cocoindex.{code_table};
            CREATE TRIGGER trg_{code_table}_search_vector
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.{code_table}
                FOR EACH ROW EXECUTE FUNCTION cocoindex.update_{code_table}_search_vector();
            UPDATE cocoindex.{code_table}
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, code, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql=f"""
            DROP TRIGGER IF EXISTS trg_{code_table}_search_vector ON cocoindex.{code_table};
            DROP FUNCTION IF EXISTS cocoindex.update_{code_table}_search_vector();
            DROP INDEX IF EXISTS cocoindex.idx_{code_table}_fts;
            ALTER TABLE cocoindex.{code_table} DROP COLUMN IF EXISTS search_vector;
        """,
    )
    for source in _sources(sources_json):
        includes = source.get("code_include", [])
        if not includes:
            continue
        root = pathlib.Path(source["root"])
        files = localfs.walk_dir(
            root,
            recursive=True,
            path_matcher=PatternFilePathMatcher(
                included_patterns=includes,
                excluded_patterns=source.get("code_exclude", []),
            ),
            live=True,
        )
        await coco.mount_each(
            coco.component_subpath(f"code_{source['tag']}"),
            process_code_file,
            files.items(),
            table,
            root,
            source["tag"],
        )


TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}


def _fetch_all_issues(repo: str) -> list[dict[str, Any]]:
    fields = "number,title,body,state,labels,createdAt,updatedAt,comments,author"
    items: list[dict[str, Any]] = []
    for kind, command in (("issue", "issue"), ("pr", "pr")):
        try:
            result = subprocess.run(
                ["gh", command, "list", "--repo", repo, "--state", "all", "--limit", "10000", "--json", fields],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode:
                log.error("gh %s list failed for %s: %s", kind, repo, result.stderr[:300])
                continue
            batch = json.loads(result.stdout)
            for item in batch:
                item["_kind"] = kind
            items.extend(batch)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            log.error("fetching %s from %s failed: %s", kind, repo, exc)
    return items


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


def _fetch_jira_issues(provider: dict[str, str]) -> list[dict[str, Any]]:
    token = _jira_token(provider["keychain_account"], provider["keychain_service"])
    if token is None:
        return []
    auth = base64.b64encode(f"{provider['email']}:{token}".encode()).decode()
    endpoint = provider["server"].rstrip("/") + "/rest/api/3/search/jql"
    fields = ["summary", "description", "status", "issuetype", "priority", "labels", "reporter", "created", "updated", "comment", "parent"]
    issues: list[dict[str, Any]] = []
    next_page_token: str | None = None
    while True:
        payload: dict[str, Any] = {
            "jql": provider["jql"],
            "maxResults": 100,
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
        if not isinstance(batch, list):
            break
        issues.extend(batch)
        if document.get("isLast", True) or not batch:
            break
        next_page_token = document.get("nextPageToken")
        if not isinstance(next_page_token, str) or not next_page_token:
            break
    log.info("Fetched %d Jira issues", len(issues))
    return issues


def _repo_name(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


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
    inner = "".join(_adf_to_text(item) for item in node.get("content", []))
    return inner + ("\n" if node_type in {"paragraph", "heading", "listItem", "codeBlock"} else "")


def _human_comments(issue: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        comment
        for comment in issue.get("comments", []) or []
        if comment.get("authorAssociation", "NONE") in TRUSTED_ASSOCIATIONS
        and not comment.get("author", {}).get("login", "").endswith("[bot]")
        and len(comment.get("body", "")) > 20
    ][:10]


@coco.fn(memo=True)
def process_issue(issue: dict[str, Any], repo: str, bank_id: str) -> None:
    number = issue.get("number", "?")
    kind = issue.get("_kind", "issue")
    short_repo = _repo_name(repo)
    label = "PR" if kind == "pr" else "Issue"
    author = issue.get("author", {}).get("login", "unknown")
    created = issue.get("createdAt", "")[:10]
    parts = [f"# {label} #{number} ({short_repo}): {issue.get('title', '')}", f"Repo: {repo} | Author: {author} | Created: {created}", ""]
    body = issue.get("body", "") or ""
    if body.strip():
        parts.extend([body.strip(), ""])
    for comment in _human_comments(issue):
        text = comment.get("body", "").strip()
        if len(text) > 2000:
            text = text[:2000] + "\n[...]"
        parts.extend([f"**@{comment.get('author', {}).get('login', '?')}:**", text, ""])
    content = "\n".join(parts)
    if len(content.strip()) < 50:
        return
    state = str(issue.get("state", "OPEN")).lower()
    labels = [item.get("name", "") for item in issue.get("labels", [])]
    base_id = f"{short_repo}-{kind}-{number}"
    sections = chunking.split_markdown_sections(content, chunk_size=1200, chunk_overlap=300)
    for section, chunk in sections:
        document_id = base_id if not section else f"{base_id}-{section}"
        hindsight_retain(
            bank_id=bank_id,
            content=chunk,
            document_id=document_id,
            timestamp=issue.get("updatedAt", ""),
            metadata={"source": "cocoindex", "repo": repo, "kind": kind, "number": str(number), "state": state},
            tags=[state, kind, short_repo] + labels[:5],
        )


@coco.fn(memo=True)
def process_jira_issue(issue: dict[str, Any], bank_id: str) -> None:
    fields = issue.get("fields", {}) or {}
    issue_key = issue.get("key", "unknown")
    summary = fields.get("summary", "")
    issue_type = (fields.get("issuetype") or {}).get("name", "Issue")
    reporter = (fields.get("reporter") or {}).get("displayName", "unknown")
    created = (fields.get("created") or "")[:10]
    parent_key = (fields.get("parent") or {}).get("key", "")
    header = f"# {issue_type} {issue_key}: {summary}\nRepo: Jira | Reporter: {reporter} | Created: {created}"
    if parent_key:
        header += f" | Parent: {parent_key}"
    description = _adf_to_text(fields.get("description")).strip()
    content = header + (f"\n\n{description}" if description else "")
    comments = ((fields.get("comment") or {}).get("comments", [])) or []
    comment_texts: list[str] = []
    for comment in comments[:10]:
        text = _adf_to_text(comment.get("body")).strip()
        if len(text) > 20:
            if len(text) > 2000:
                text = text[:2000] + "\n[...]"
            author = (comment.get("author") or {}).get("displayName", "?")
            comment_texts.append(f"**{author}:**\n{text}")
    if len(content.strip()) < 50:
        return
    status = ((fields.get("status") or {}).get("name") or "unknown").lower()
    labels = fields.get("labels", []) or []
    issue_type_tag = str((fields.get("issuetype") or {}).get("name", "issue")).lower()
    sections = chunking.split_issue_sections(content, comment_texts, chunk_size=1200, chunk_overlap=300)
    for suffix, chunk in sections:
        document_id = f"jira-{issue_key}" if not suffix else f"jira-{issue_key}-{suffix}"
        hindsight_retain(
            bank_id=bank_id,
            content=chunk,
            document_id=document_id,
            timestamp=fields.get("updated", ""),
            metadata={"source": "cocoindex", "tracker": "jira", "key": str(issue_key), "status": status},
            tags=[status, issue_type_tag, "jira"] + [str(label) for label in labels[:5]],
        )


@coco.fn
def issues_main(provider_json: str, bank_id: str) -> None:
    provider = json.loads(provider_json)
    if provider["kind"] == "jira":
        for issue in _fetch_jira_issues(provider):
            process_jira_issue(issue, bank_id)
        return
    for repo in provider["repos"]:
        for issue in _fetch_all_issues(repo):
            process_issue(issue, repo, bank_id)


def build_apps(config: ProjectAdapterConfig) -> dict[str, coco.App]:
    """Build the selected project's apps from validated TOML values."""
    apps: dict[str, coco.App] = {}
    if config.docs_bank and config.docs_sources:
        apps["docs"] = coco.App(
            f"{config.project}-docs",
            docs_main,
            sources_json=_source_json(config.docs_sources),
            bank_id=config.docs_bank,
        )
    if config.code_table and config.code_sources:
        apps["code"] = coco.App(
            f"{config.project}-code",
            code_main,
            sources_json=_source_json(config.code_sources),
            code_table=config.code_table,
        )
    if config.issues_bank and (config.issues_repos or config.issues_provider == "jira"):
        if config.issues_provider == "jira":
            provider_json = json.dumps({
                "kind": "jira",
                "server": config.jira_server,
                "email": config.jira_email,
                "jql": config.jira_jql,
                "keychain_account": config.jira_keychain_account,
                "keychain_service": config.jira_keychain_service,
            })
        else:
            provider_json = json.dumps({"kind": "github", "repos": list(config.issues_repos)})
        apps["issues"] = coco.App(
            f"{config.project}-issues",
            issues_main,
            provider_json=provider_json,
            bank_id=config.issues_bank,
        )
    return apps


def _run_live(config: ProjectAdapterConfig, apps: dict[str, coco.App], selected: set[str]) -> None:
    threads: list[threading.Thread] = []
    for name in ("docs", "code"):
        if name not in selected or name not in apps:
            continue

        def run_file_app(app_name=name, app=apps[name]):
            try:
                app.update_blocking(live=True)
            except Exception:
                log.exception("%s app crashed", app_name)

        thread = threading.Thread(target=run_file_app, name=f"engram-{config.project}-{name}", daemon=True)
        thread.start()
        threads.append(thread)

    if "issues" in selected and "issues" in apps:

        def poll_issues():
            while True:
                try:
                    apps["issues"].update_blocking()
                except Exception:
                    log.exception("issues app failed")
                time.sleep(config.issues_poll_seconds)

        thread = threading.Thread(target=poll_issues, name=f"engram-{config.project}-issues", daemon=True)
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()


def main() -> None:
    parser = argparse.ArgumentParser(description="Configuration-driven Engram project flow")
    parser.add_argument("--project", required=True, help="Project key in projects.toml")
    parser.add_argument("--config", help="Path to projects.toml (defaults to ENGRAM_PROJECTS_CONFIG)")
    parser.add_argument("--mode", choices=["backfill", "live"], default="live")
    parser.add_argument("--apps", nargs="*", choices=["docs", "issues", "code"], default=None)
    args = parser.parse_args()

    config = load_adapter_config(args.project, args.config)
    configure(config)
    apps = build_apps(config)
    selected = set(args.apps) if args.apps else set(apps)
    unknown = selected - apps.keys()
    if unknown:
        parser.error(f"project {config.project!r} has no configured app(s): {', '.join(sorted(unknown))}")
    if not selected:
        parser.error(f"project {config.project!r} has no runnable apps")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    _wait_for_hindsight()
    if args.mode == "backfill":
        for name in ("docs", "issues", "code"):
            if name in selected:
                apps[name].update_blocking(report_to_stdout=True)
    else:
        _run_live(config, apps, selected)


if __name__ == "__main__":
    main()
