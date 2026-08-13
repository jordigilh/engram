"""Fixtures for the integration tier -- tests marked @pytest.mark.integration
that talk to a real, disposable Postgres+pgvector instance. Never the shared
dev Postgres on 5432 (see docs/FINDINGS.md 2026-08-02/03 for why: an
unisolated dev/test migration run against that instance caused a real
production outage).

Two provisioning paths, one contract (the IT_POSTGRES_URL connection
string):

- CI (GitHub Actions): the `integration-tests` job's `services:` stanza
  already has a pgvector/pgvector:pg16 container running and sets
  IT_POSTGRES_URL before pytest starts -- this fixture just reuses it.
- Local dev: IT_POSTGRES_URL is unset, so this fixture shells out to
  `podman` to spin up its own disposable container on a random free host
  port, waits for it to accept connections, and tears it down at the end of
  the test session. Requires a running podman machine locally (macOS:
  `podman machine start`) -- see tests/integration/README.md.

Test bodies never need to know which path provisioned the database.
"""
from __future__ import annotations

import os
import subprocess
import time

import psycopg2
import pytest

PGVECTOR_IMAGE = "docker.io/pgvector/pgvector:pg16"
CONTAINER_READY_TIMEOUT_S = 30


def _wait_until_ready(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = psycopg2.connect(url, connect_timeout=2)
            conn.close()
            return
        except psycopg2.OperationalError as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"Postgres at {url} did not become ready within {timeout_s}s: {last_error}")


def _spawn_local_container() -> tuple[str, str]:
    """Starts a disposable pgvector/pgvector:pg16 container on a random free
    host port via podman. Returns (connection_url, container_id)."""
    result = subprocess.run(
        [
            "podman", "run", "-d",
            "-p", "127.0.0.1::5432",
            "-e", "POSTGRES_PASSWORD=postgres",
            PGVECTOR_IMAGE,
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"podman run failed (is `podman machine start` running locally?): {result.stderr}"
        )
    container_id = result.stdout.strip()

    port_result = subprocess.run(
        ["podman", "port", container_id, "5432/tcp"],
        capture_output=True, text=True, timeout=15,
    )
    if port_result.returncode != 0:
        subprocess.run(["podman", "rm", "-f", container_id], capture_output=True)
        raise RuntimeError(f"podman port failed: {port_result.stderr}")
    # Output shape: "127.0.0.1:54321"
    host_port = port_result.stdout.strip().rsplit(":", 1)[-1]

    url = f"postgresql://postgres:postgres@127.0.0.1:{host_port}/postgres"
    return url, container_id


@pytest.fixture(scope="session")
def pg_url() -> str:
    existing = os.environ.get("IT_POSTGRES_URL")
    if existing:
        _wait_until_ready(existing, CONTAINER_READY_TIMEOUT_S)
        yield existing
        return

    url, container_id = _spawn_local_container()
    try:
        _wait_until_ready(url, CONTAINER_READY_TIMEOUT_S)
        yield url
    finally:
        subprocess.run(["podman", "rm", "-f", container_id], capture_output=True)


@pytest.fixture(scope="session")
def _vector_extension(pg_url: str) -> None:
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute("CREATE SCHEMA IF NOT EXISTS cocoindex;")
    finally:
        conn.close()


# Mirrors src/engram/flows/dcm.py's CodeEmbedding dataclass + the
# fts_search_vector SQL attachment's setup_sql (lines ~553-665) *exactly*,
# down to the real table/function/trigger names -- this is what lets the
# test file call search/dcm.py's real search_code() completely unmodified
# (only PG_URL and _embed_query are patched) and have its hardcoded
# "cocoindex.dcm_code_embeddings" table references just work. Safe to reuse
# the production name here because this schema lives in the disposable
# per-session container, never the real dev Postgres.
#
# The vector column is declared vector(4) rather than production's
# vector(384) (all-MiniLM-L6-v2's real output dimension) to keep test
# fixtures small and hand-computable -- search_code() itself never hardcodes
# a dimension, it just casts whatever _embed_query() returns to ::vector, so
# this is a faithful stand-in for the SQL/pgvector behavior under test.
_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS cocoindex.dcm_code_embeddings (
        id text PRIMARY KEY,
        filepath text,
        chunk_index int,
        code text,
        embedding vector(4),
        search_text text
    );

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
"""

_TRUNCATE_SQL = "TRUNCATE TABLE cocoindex.dcm_code_embeddings;"


@pytest.fixture
def code_table(pg_url: str, _vector_extension: None):
    """Real cocoindex.dcm_code_embeddings table (production schema/trigger,
    verbatim) truncated before/after each test so tests stay isolated
    within the one shared session container."""
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
            cur.execute(_TRUNCATE_SQL)
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(_TRUNCATE_SQL)
        conn.close()
