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
