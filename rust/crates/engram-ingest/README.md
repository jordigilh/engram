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
  implementation, using sparse TF-IDF rows for sentence similarity.
- Lease/retry/recovery state machine and a convergence harness against a real
  Postgres.
- Hindsight sink: retain POSTs, document DELETEs for tombstones, ledger-owned
  retries, and `sink_deleted_at` acknowledgement.
- Bank-scoped worker loop: bounded per-bank slot concurrency, jittered idle and
  error backoff, periodic expired-lease recovery, startup recovery, per-bank
  counters, and graceful cancellation.

## Not in this slice

- Filesystem watcher hints. `reconcile` consumes a scan snapshot; the watcher
  adapter is a later ingestion slice.
- Native-vs-CocoIndex shadow comparison of emitted operations (the remaining
  part of issue #214 slice 3).
- Issues, transcripts, PDF extraction, git sync, CocoIndex/Python removal.
- Supervisor-level respawn/restart isolation. The worker exposes per-slot health
  (`WorkerHandle::health`) but restart policy lands with the service switch.

PR map: the merged ledger/docs PR, the merged Hindsight sink, then this worker
loop/concurrency/metrics slice, issues/transcripts, service switch, and
Python/CocoIndex removal (#232).

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
- **Apply is retry-idempotent and revision-fenced.** Sources carry a monotonic
  revision. A matching immediate replay is a no-op; an older plan is rejected
  with `StaleReconciliation`, and deletes re-check the planned generation.
  Jobs carry a deterministic dedupe key (retain: retain revision + payload hash;
  delete: chunk generation + hash) and tombstones a unique
  `(source, document id, reason, generation)`. A retried retain and an
  A→B→A payload return both enqueue exactly one new sink job.
- **Per-document jobs run in ledger order.** A pending earlier job (including
  one in retry backoff) blocks later jobs for the same document; a stale delete
  can never run after a newer restore. Claims carry an opaque token, so a
  delayed completion from a reaped worker cannot touch the replacement lease.
- **One HTTP attempt per ledger job.** The durable job ledger owns retries
  (1s/2s, three attempts) so a crash between Hindsight and the acknowledgement
  cannot multiply attempts. Retain uses the 60s timeout; delete uses 10s and
  treats 404 as already-absent success, matching the Python delete contract.
  A successful delete acknowledges matching tombstones via `sink_deleted_at`.
- **Renames that keep a base id transfer ownership.** The old path's delete is
  dropped when the same document id is retained by a live path; two live paths
  producing one id are rejected as a scan conflict.
- **Whitespace-only files emit nothing**, matching `process_doc_file`'s early
  return. Previously retained chunks are not deleted.
- **`source=cocoindex` metadata is preserved** by contract
  (`docs/COCOINDEX_DEPRECATION.md:98`) until a bank reindex changes the value.
- **Banks are explicit and immutable per source.** `sources.bank_id` comes from
  the configured docs bank; the v3 migration never guesses one. A failed bank
  guard stops `migrate()`/worker startup with an actionable message, and changing
  an existing source's bank is rejected (`BankChanged`) rather than silently
  orphaning old-bank memories.
- **Jobs are bank-scoped and serialized per bank document.** Each job snapshots
  its source bank. Claims, recovery, and per-bank metrics never cross that
  boundary, and two sources in one bank that share a Hindsight document id
  cannot hold it concurrently. A multi-bank ledger is safe with one supervisor
  over all banks.
- **Worker concurrency is bounded per bank.** Each slot owns one Postgres
  connection and shares one HTTP client. Defaults are intentionally small; the
  hard cap is 8 slots/bank and 32 total.
- **Recovery is periodic, not tied to claims.** Startup runs configured-set
  recovery; runtime recovery runs on an interval even while other jobs are
  claimable. Expired leases for a bank absent from the worker config are
  requeued without consuming an attempt (`stranded`), so a deconfigured bank
  cannot burn its retry budget while no worker serves it.
- **Delivery is at-least-once.** A lost ack is reported as `LeaseLost` and
  counted separately; a re-send of the same stable `document_id` converges. The
  worker re-checks its lease immediately before the Hindsight mutation, and the
  lease TTL is at least twice the request timeout, so a normal in-flight call is
  fenced. Hindsight has no per-job generation contract, so a worker paused past
  its lease can still deliver one stale operation before the replacement worker
  runs; that edge is the accepted at-least-once/ordering limit of the current
  Hindsight API. An expired/lost lease consumes one durable attempt, so a job
  that repeatedly crashes a worker is bounded and eventually reaches `dead`. The
  `changed` counter belongs to reconciliation (`jobs_created`), not the sink.
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

`migrate()` bootstraps `engram_ingest.schema_migrations`, applies every embedded
migration version once in order under an advisory lock, and then verifies the
columns, constraints, and partial index the code depends on. It refuses a
database that records a version the binary does not embed. Migration `0002`
adds `jobs.last_error_class`; migration `0004` replaces the per-source lease
index with `jobs_one_leased_per_bank_document_idx` and requeues surplus leased
jobs before building it. The v1→v2 and v2→v4 upgrades are exercised by the
Postgres harnesses.

Source identity is the unique `(project, tag, root)`. Chunks are scoped by
`source_id` with unique `(source_id, hindsight_document_id)` and
`(document_id, chunk_key)`, so two projects with the same tag/path can share a
Hindsight document id without touching each other's rows. That is the design's
`(project, source identity, unit key)` constraint.

`migrate()` also runs an unversioned bank guard on every call. Migration `0003`
adds nullable `sources.bank_id` / `jobs.bank_id` and the partial
`jobs_bank_pending_idx`. If an existing source has no bank, migration fails with
`MigrationGuard` telling the operator to set `sources.bank_id` explicitly in
their own connection; rerunning then backfills jobs, enforces `NOT NULL`, and
verifies the CHECK constraints and index.

## Running the Postgres harness

The harness needs a disposable database whose name contains `test`:

```bash
createdb -h /tmp -U hindsight engram_ingest_test
ENGRAM_INGEST_TEST_DATABASE_URL='postgresql://hindsight@/engram_ingest_test?host=/tmp' \
  cargo test -p engram-ingest --test ledger_postgres --test sink_postgres \
             --test worker_postgres
```

It truncates the `engram_ingest` tables and refuses any database whose name is
not exactly `engram_ingest_test`. CI runs it against a `pgvector/pgvector:pg16`
service. Without the URL the Postgres harnesses skip by design (integration
tier); the 90% coverage CI gate fails in that state, so a local skip cannot
masquerade as coverage.

## Local gates

```bash
cargo fmt --all --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --all --locked
cargo +1.88.0 check --all-targets --locked
```
