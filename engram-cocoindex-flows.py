#!/usr/bin/env python3
"""CocoIndex flows for Engram's own repo — incremental ingestion into Hindsight
and pgvector.

Declares two apps (no issues app — this repo has zero GitHub issues; decisions
and bug tracking happen in docs/FINDINGS.md instead):
  1. engram-repo-docs: Markdown docs (docs/*.md) -> Hindsight engram-docs bank
  2. engram-repo-code: Python source (*.py)      -> pgvector engram_code_embeddings table

Runs as a single long-lived process via launchd. Supports backfill and live modes.

Note on naming: kubernaut's cocoindex-flows.py already uses "engram-docs"/
"engram-code" as internal CocoIndex App() identifiers (a historical leftover
from when "Engram" and "the kubernaut flow" were the same codebase — see
docs/FINDINGS.md). This file deliberately uses "engram-repo-*" for its own
App() names to avoid confusing log-line collisions with that unrelated file.
"""

import argparse
import dataclasses
import logging
import os
import pathlib
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
log = logging.getLogger("engram-cocoindex-flows")

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")

ENGRAM_REPO_DIR = pathlib.Path(os.environ.get(
    "ENGRAM_REPO_DIR",
    os.path.expanduser("~/go/src/github.com/jordigilh/engram"),
))

PG_DSN = os.environ.get(
    "COCOINDEX_PG_URL",
    "postgresql://hindsight:hindsight@localhost:5432/hindsight",
)
COCOINDEX_DB = pathlib.Path(os.environ.get(
    "COCOINDEX_DB",
    os.path.expanduser("~/.hindsight/engram-cocoindex.db"),
))

# Named distinctly from cocoindex-flows.py's own ContextKey("pg_pool") -- both
# run as separate launchd processes today, but CocoIndex registers
# ContextKeys process-globally and raises ValueError on a same-name second
# registration, so anything that ever imports both modules into one process
# (e.g. the pytest suite) would crash on collection. See the App()-naming
# note above for the same collision class.
PG_POOL: coco.ContextKey[Any] = coco.ContextKey("engram_repo_pg_pool")


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
    import json

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
# App 1: engram-repo-docs — Markdown docs -> Hindsight engram-docs bank
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
            bank_id="engram-docs",
            content=chunk,
            document_id=doc_id,
            timestamp=timestamp,
            metadata={"source": "cocoindex", "repo": source_tag},
            tags=[section, source_tag],
        )


@coco.fn
async def docs_main(docs_dir: pathlib.Path) -> None:
    docs = localfs.walk_dir(
        docs_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*.md"]),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("docs"),
        process_doc_file, docs.items(),
        docs_dir, "engram",
    )


docs_app = coco.App(
    "engram-repo-docs", docs_main,
    docs_dir=ENGRAM_REPO_DIR / "docs",
)


# ---------------------------------------------------------------------------
# App 2: engram-repo-code — Python source -> pgvector engram_code_embeddings
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
async def code_main(code_dir: pathlib.Path) -> None:
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
        PG_POOL, "engram_code_embeddings", schema, pg_schema_name="cocoindex",
    )
    table.declare_vector_index(column="embedding", metric="cosine")

    table.declare_sql_command_attachment(
        name="fts_search_vector",
        setup_sql="""
            ALTER TABLE cocoindex.engram_code_embeddings
                ADD COLUMN IF NOT EXISTS search_vector tsvector;

            CREATE INDEX IF NOT EXISTS idx_engram_code_embeddings_fts
                ON cocoindex.engram_code_embeddings USING gin(search_vector);

            CREATE OR REPLACE FUNCTION cocoindex.update_engram_code_search_vector()
            RETURNS trigger AS $$
            BEGIN
                NEW.search_vector := to_tsvector('simple',
                    coalesce(NEW.search_text, '') || ' ' || coalesce(NEW.filepath, ''));
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_engram_code_search_vector
                ON cocoindex.engram_code_embeddings;
            CREATE TRIGGER trg_engram_code_search_vector
                BEFORE INSERT OR UPDATE OF search_text, filepath
                ON cocoindex.engram_code_embeddings
                FOR EACH ROW
                EXECUTE FUNCTION cocoindex.update_engram_code_search_vector();

            UPDATE cocoindex.engram_code_embeddings
            SET search_vector = to_tsvector('simple',
                coalesce(search_text, code, '') || ' ' || coalesce(filepath, ''))
            WHERE search_vector IS NULL;
        """,
        teardown_sql="""
            DROP TRIGGER IF EXISTS trg_engram_code_search_vector
                ON cocoindex.engram_code_embeddings;
            DROP FUNCTION IF EXISTS cocoindex.update_engram_code_search_vector();
            DROP INDEX IF EXISTS cocoindex.idx_engram_code_embeddings_fts;
            ALTER TABLE cocoindex.engram_code_embeddings
                DROP COLUMN IF EXISTS search_vector;
        """,
    )

    files = localfs.walk_dir(
        code_dir,
        recursive=True,
        path_matcher=PatternFilePathMatcher(
            included_patterns=["**/*.py"],
            excluded_patterns=[
                "**/__pycache__/**",
                "**/.pytest_cache/**",
                "**/.git/**",
                "**/venv/**",
                "**/node_modules/**",
            ],
        ),
        live=True,
    )
    await coco.mount_each(
        coco.component_subpath("engram"),
        process_code_file, files.items(),
        table, code_dir, "engram",
    )


code_app = coco.App(
    "engram-repo-code", code_main,
    code_dir=ENGRAM_REPO_DIR,
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

        t = threading.Thread(target=_run_app, name=f"engram-{name}", daemon=True)
        t.start()
        threads.append(t)

    log.info("All %d apps launched, watching files", len(threads))

    for t in threads:
        t.join()


def main():
    parser = argparse.ArgumentParser(
        description="CocoIndex flows for Engram's own repo — incremental ingestion"
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
        choices=["docs", "code"],
        default=None,
        help="Run only specific apps (default: all)",
    )
    args = parser.parse_args()

    selected = set(args.apps) if args.apps else {"docs", "code"}

    log.info("Starting Engram CocoIndex in %s mode — apps: %s", args.mode, ", ".join(sorted(selected)))
    log.info("  Repo dir:      %s", ENGRAM_REPO_DIR)
    log.info("  Hindsight URL: %s", HINDSIGHT_URL)
    log.info("  CocoIndex DB:  %s", COCOINDEX_DB)

    _wait_for_hindsight()

    if args.mode == "backfill":
        for name, app in [("docs", docs_app), ("code", code_app)]:
            if name in selected:
                log.info("Running %s app backfill...", name)
                app.update_blocking(report_to_stdout=True)
                log.info("%s app backfill complete", name)
        log.info("All backfills finished")
    else:
        _run_live(selected)


if __name__ == "__main__":
    main()
