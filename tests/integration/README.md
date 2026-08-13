# Integration tests

Tests here are marked `@pytest.mark.integration` and talk to a real
Postgres+pgvector instance. They are excluded from the default `pytest`
run (see `[tool.pytest.ini_options]` in `pyproject.toml`) and from the
`lint-and-test` CI job, which runs `pytest tests/ -m "not integration"`.

**Never point these at the shared dev Postgres on `localhost:5432`.** See
`docs/FINDINGS.md` (2026-08-02/03) for why: an unisolated test run against
that instance previously caused a production outage. Every test in this
directory operates against a disposable, single-purpose container.

## Running in CI

Nothing to do -- the `integration-tests` job in
`.github/workflows/ci.yml` starts a `pgvector/pgvector:pg16` service
container and sets `IT_POSTGRES_URL` before invoking pytest. The
`tests/integration/conftest.py` fixtures detect and reuse it automatically.

## Running locally

Requires a running Podman machine (macOS: `podman machine start`). If
`IT_POSTGRES_URL` is unset, the `pg_url` fixture in `conftest.py` shells
out to `podman run` to spin up its own disposable `pgvector/pgvector:pg16`
container on a random free host port for the test session, and tears it
down afterward -- no manual setup needed beyond having Podman itself
running:

```bash
podman machine start   # once per shell session / reboot, if not already up
pytest tests/integration/ -m integration -q
```

To point at a container you're managing yourself instead (e.g. for faster
iteration -- skips the per-session spin-up/teardown), set
`IT_POSTGRES_URL` explicitly and the fixture will reuse it as-is:

```bash
podman run -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16
IT_POSTGRES_URL=postgresql://postgres:postgres@127.0.0.1:5433/postgres \
    pytest tests/integration/ -m integration -q
```

## Known local environment issue (2026-08-12)

On at least one dev machine, `podman machine start` reports success while
Podman Desktop.app is running, but the VM reverts to `stopped` within
seconds (no `vfkit` process ever stays up) -- Podman Desktop's own machine
lifecycle management appears to race with CLI-driven start/stop. If
`podman run` fails with a connection error in the `pg_url` fixture, quit
Podman Desktop.app first and retry `podman machine start` from the CLI
alone.
