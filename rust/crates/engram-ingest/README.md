# `engram-ingest`

Native ingestion core for the CocoIndex replacement (issue #214, slice 2 plus
the docs-parity half of slice 3).

## What this crate owns

- Postgres ledger: `sources`, `documents`, `chunks`, `jobs`, `leases`,
  `tombstones`, and `schema_migrations` in the `engram_ingest` schema.
- SHA-256 reconciliation of a scan snapshot against the stored manifest.
- Stable docs base IDs and heading-hash chunk keys, including the heading-less
  `chunk{i}` fallback.
- Deterministic synthesis (`key_sentences`, `keywords`) matching the Python
  implementation.
- Lease/retry/recovery state machine and a convergence harness against a real
  Postgres.

## Not in this slice

- Hindsight HTTP retain/delete. Jobs store the retain payload; the sink worker
  lands with the Hindsight parity slice.
- Filesystem watcher hints. `reconcile` consumes a scan snapshot; the watcher
  adapter is a later ingestion slice.
- Native-vs-CocoIndex shadow comparison of emitted operations (the remaining
  part of issue #214 slice 3).
- Issues, transcripts, PDF extraction, git sync, CocoIndex/Python removal.

PR map: this PR is ledger + docs parity. Later PRs: Hindsight sink parity,
shadow run, issues/transcripts, service switch, then Python/CocoIndex removal
(#232).

## Parity and deliberate behavior

- **Emitted operations are file-scoped.** Python builds synthesis metadata from
  the whole document and retains every chunk of a changed file
  (`configured.py:229-248`), so a changed file here re-emits every chunk with
  fresh metadata. Hindsight's content hashing still avoids rewriting unchanged
  chunk content downstream. Per-chunk retain skipping is deferred until
  metadata becomes chunk-scoped.
- **Unchanged files emit nothing.** A file counts as unchanged only when its
  SHA-256, layout version, and active chunk set match the scan, so a partial
  apply is repaired on the next pass.
- **Duplicate heading lines collapse to the last section.** Python emits two
  retains with the same document id; Hindsight's final state is the later one,
  so the ledger keeps one chunk per id (last occurrence) and replay converges.
- **Apply is retry-idempotent.** Jobs carry a deterministic dedupe key (retain:
  payload hash + generation; delete: chunk generation + hash) and tombstones a
  unique `(source, document id, reason, generation)`, so re-applying the same
  reconciliation after a lost commit acknowledgement adds no jobs or tombstones.
- **Per-document jobs run in ledger order.** A pending earlier job (including
  one in retry backoff) blocks later jobs for the same document; a stale delete
  can never run after a newer restore.
- **Whitespace-only files emit nothing**, matching `process_doc_file`'s early
  return. Previously retained chunks are not deleted.
- **`source=cocoindex` metadata is preserved** by contract
  (`docs/COCOINDEX_DEPRECATION.md:98`) until a bank reindex changes the value.
- **Python 3.14 is authoritative** for `pathlib` suffix semantics; Python 3.13
  differs only for trailing-dot filenames. See `fixtures/README.md`.
- **No Python tests were added.** The fixture generator is developer tooling
  under `rust/`, is not collected by pytest, and is ruff-clean.

## Ledger mapping

| Design name (`NATIVE_ENGRAM_DESIGN.md`) | This crate |
| --- | --- |
| `schema_migrations` | `engram_ingest.schema_migrations` |
| `source_records` | `engram_ingest.sources` |
| `source_units` | `engram_ingest.documents` + `engram_ingest.chunks` |
| `ingest_jobs` | `engram_ingest.jobs` + `engram_ingest.leases` |
| `transcript_watermarks`, `gateway_events` | deferred to those slices |

`migrate()` applies the single unreleased v1 schema and then verifies the
columns the code needs; a schema created by an earlier shape of `0001` fails
with `MigrationMismatch` instead of breaking later. Recreate the schema while
this slice is unreleased; versioned upgrade steps start with the first release.

Source identity is the unique `(project, tag, root)`. Chunks are scoped by
`source_id` with unique `(source_id, hindsight_document_id)` and
`(document_id, chunk_key)`, so two projects with the same tag/path can share a
Hindsight document id without touching each other's rows. That is the design's
`(project, source identity, unit key)` constraint.

## Running the Postgres harness

The harness needs a disposable database whose name contains `test`:

```bash
createdb -h /tmp -U hindsight engram_ingest_test
ENGRAM_INGEST_TEST_DATABASE_URL='postgresql://hindsight@/engram_ingest_test?host=/tmp' \
  cargo test -p engram-ingest --test ledger_postgres
```

It truncates the `engram_ingest` tables and refuses any database name without
`test`. CI runs it against a `pgvector/pgvector:pg16` service.

## Local gates

```bash
cargo fmt --all --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --all --locked
cargo +1.88.0 check --all-targets --locked
```
