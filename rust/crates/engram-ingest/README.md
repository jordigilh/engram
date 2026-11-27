# `engram-ingest`

Native ingestion core for the CocoIndex replacement (issue #214, slices 2-5:
ledger/docs, Hindsight sink, worker loop, GitHub/Jira issues, and GitHub
discussions/project boards).

## What this crate owns

- Postgres ledger: `sources`, `documents`, `chunks`, `jobs`, `leases`,
  `tombstones`, `issue_cursors`, and `schema_migrations` in the
  `engram_ingest` schema.
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
- GitHub/Jira issue ingestion: direct REST/ADF serialization, full-snapshot
  polling with pagination, ETag single-page fast paths, rate-limit handling,
  per-stream cursors, and out-of-scope deletion through the same ledger.
- GitHub discussions and project boards: GraphQL pagination, stable discussion
  IDs and comment keys, and one-document board grouping with the Python status
  order.
- Transcript ingestion: JSONL parsing, Python-compatible learning windows and
  SHA-1 window ids, durable ack watermarks, append replay, truncation/rotation
  tolerance, and the existing ledger/worker path for retain and tombstones.
- Runtime entrypoint: project config/source selection, deterministic docs scan,
  startup and periodic scan → reconcile → apply for docs **and transcript
  sources**, one shared Hindsight sink, the worker supervisor (docs and
  transcript banks), SIGINT/SIGTERM shutdown, and a `--once` drain mode.

## Not in this slice

- Filesystem watcher hints. The runtime uses a bounded periodic reconciliation
  scan; event debounce/hints are a later ingestion slice.
- PDF extraction. A `document_format = "pdf"` source is rejected at startup
  until the Rust extractor lands.
- LLM-backed contradiction resolution. The transcript path runs the
  deterministic `RegexSignalDetector` and `RetainResolver` (retain/no-op) in
  Shadow mode; the `ContradictionResolver`/`MemoryInvalidator` traits stay
  injectable for a later classifier slice, and no external LLM calls or
  dependencies are added.
- Git sync and CocoIndex/Python removal.
- Supervisor-level respawn/restart isolation. The runtime exposes per-slot health
  (`WorkerHandle::health`) and drains on shutdown; restart policy lands with the
  service switch.

PR map: the merged ledger/docs PR, the merged Hindsight sink, the worker
loop/concurrency/metrics slice, issues (#243), discussions/boards (#244),
transcripts, service switch, and Python/CocoIndex removal (#232).

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
- **No Python tests were added.** The fixture generator and docs shadow tooling
  under `rust/` are developer tooling, are not collected by pytest, and are
  ruff-clean.

### Issues

- **Full snapshots, not deltas.** Every poll fetches the whole configured
  GitHub repo/item-kind or Jira JQL result set, matching Python's
  `issues_main`. A delta fed to `reconcile` would delete every unchanged issue.
  Hitting the configured `limit` with more pages left aborts the poll instead of
  reconciling a truncated snapshot.
- **ETags are single-page only.** A stored ETag is replayed only when the prior
  snapshot fit in one page; a 304 on page 1 cannot prove later pages unchanged.
  The ETag is cleared before a snapshot is applied, so a crash mid-apply cannot
  replay a 304 for the old list.
- **Multi-page GitHub snapshots prove stability before deleting.** Pages are
  offset-based, so a second full pass must return the same ordered issue
  identities. Only then are `issue_removed` tombstones allowed; otherwise the
  snapshot still retains changed issues but cannot delete absent ones.
- **Rate limits use both headers.** `Retry-After` and `x-ratelimit-reset` are
  combined to the later deadline.
- **Bank streams are validated together.** `validate_bank_streams` rejects
  duplicate identities, shared Jira prefixes, shared discussion
  `(prefix, short_repo)` scopes, and shared board `(prefix, number)` scopes.
- **One source row per stream.** GitHub `(repo, item kind, prefix)` and Jira
  `(server, project, JQL, prefix)` are separate sources, keyed by a config hash,
  so a failed repo or a changed JQL cannot silently alias another stream.
- **Ordinal comment keys are preserved.** Comment documents stay
  `{base}-comment{i}` (and `-part{n}`) to keep existing Hindsight ids. Stable
  comment-id keys are a separate reindex, not a silent id change.
- **`gh` shape is normalized from REST.** Bot logins lose the `[bot]` suffix,
  merged PRs derive `merged` from `pull_request.merged_at`, and
  `milestone.due_on` maps to the Python `dueOn` field. Those three quirks keep
  REST output identical to the existing `gh`-fed documents.
- **Snapshot hash includes metadata.** State, labels, milestone, updated
  timestamp, and comments are hashed alongside the header/body, so a close or
  relabel re-retains tags/metadata even though the header text is stable.
- **An empty result set is authoritative.** That is what makes out-of-scope
  deletion exact, but a Jira service account that loses browse permission can
  also return `issues: []` with `200`. Keep JQL scoped to the intended project
  and watch `issue_cursors.item_count`; a sudden drop to zero is the signal to
  verify credentials before accepting the tombstone pass.

### Discussions and boards

- **Discussion ids match Python.** `[{prefix}-]{short_repo}-discussion-{number}`,
  with `comment{i}` / `part{n}` keys and no `state` metadata field.
- **Discussion comments are first-ten-raw.** The first ten GraphQL comment
  nodes are selected, then body length and the literal `[bot]` suffix are
  checked. Surviving comments are renumbered, exactly like Python. A bot login
  without the suffix such as `praxis-bot` remains eligible.
- **Discussions paginate and prove stability before deleting.** A truncated
  limit disables deletions; multi-page results require two matching ordered
  identity passes.
- **Boards are one document.** The literal slug
  `lower().replace(" ", "-").replace("/", "-")` and the Python status order are
  preserved. `Total items` counts every GraphQL node, including drafts with no
  content. Board timestamps are excluded from the snapshot hash so a poll does
  not re-retain unchanged boards.
- **GraphQL failures are not empty snapshots.** Non-empty `errors`, a null
  repository/project, or a missing board title returns an error before apply.

### Transcripts

- **Windows match Python in regex mode.** The signal is a user correction
  (`CORRECTION_PATTERNS`, regex mode) or instruction (`INSTRUCTION_PATTERNS`);
  context is two parsed messages each way; each line is the Python
  `"User: ..."`/`"Assistant: ..."` rendering truncated to 300 characters.
  Window ids are `transcript-{stem}-w{sha1(window)[:16]}`. The Haiku classifier
  plugs into the `SignalDetector` trait; the default keeps the documented regex
  fallback so tests are deterministic and offline.
- **Only new signals are emitted.** The watermark is the parsed-message count.
  Before-context still comes from the full file.
- **Rewrites restart from zero and tombstone vanished windows.** A stored
  SHA-256 of the parsed prefix detects truncation, equal-length rewrites, and
  rotation in place. The scan then plans from index 0 and deletes active windows
  that no longer exist in the new content. Normal appends keep historical
  windows, matching Python's append-only behavior.
- **The watermark is acknowledged, not enqueued.** A scan refresh advances it
  only when every transcript retain job is succeeded; a pending/leased/dead job
  caps it at the last contiguous ack. A failed retain therefore leaves it
  unadvanced while the job state remains visible.
- **Replay is idempotent.** A durable `transcript_windows` row prevents replay
  from re-running contradiction resolution or duplicating a withheld window.
- **Scope removal tombstones.** A removed transcript file deletes its active
  windows with reason `transcript_removed` and drops its watermark and window
  rows.
- **Contradiction resolution is injected and durable.** `Queued` withholds the
  window and records status `queued`; `AutoResolved` adds `CORRECTION,
  supersedes-prior-memory`. Only `Live` mode records a pending invalidation,
  which runs only after a successful ledger apply and retries until the PATCH is
  acknowledged. The resolver owns Python's audit log and pending-review queue.
- **Correction dispatch intentionally matches Python's substring gate.** Python
  checks `"[CORRECTION]" in window_text`, including the edge case where a
  message body contains that literal; the native path keeps the same behavior
  rather than adding a divergent signal-kind branch.
- **Malformed/partial lines are skipped.** A concurrent write ending in an
  incomplete JSON line is ignored until the line is valid; it never advances the
  watermark past unread data. Invalid UTF-8 skips that file and leaves its
  watermark untouched (stricter than Python's task-wide failure).
- **Identity is path-scoped for state, stem-scoped for ids.** Watermarks and
  ownership are keyed by root-relative path, while window ids keep the Python
  `transcript-{stem}` prefix. A same-stem file whose window id is already owned
  by another path is skipped for that scan instead of failing the whole source.
- **Discovery stays inside the root.** Directory symlinks are followed only
  when their canonical target remains under the configured root, and a visited
  set prevents cycles. Reads are capped at 64 MiB.

A `dead` transcript retain is terminal by design: the watermark stays put and
the window is not re-enqueued. Redrive is an explicit operator action after the
sink cause is fixed. The store exposes
`redrive_dead_transcript_jobs(client, source_id)` for the service slice; it
locks the source row first so a concurrent reconciliation/restore cannot race
the requeue. The equivalent SQL must do the same:

```sql
BEGIN;
SELECT id FROM engram_ingest.sources WHERE id = <source_id> FOR UPDATE;
UPDATE engram_ingest.jobs j
   SET state = 'pending', attempts = 0, run_after = now(), updated_at = now()
 WHERE j.source_id = <source_id> AND j.state = 'dead'
   AND (
       (j.kind = 'retain' AND EXISTS (
           SELECT 1 FROM engram_ingest.chunks c
            WHERE c.source_id = j.source_id
              AND c.hindsight_document_id = j.hindsight_document_id
              AND c.state = 'active'
              AND c.generation = j.generation
       ))
       OR (j.kind = 'delete' AND EXISTS (
           SELECT 1 FROM engram_ingest.chunks c
            WHERE c.source_id = j.source_id
              AND c.hindsight_document_id = j.hindsight_document_id
              AND c.state = 'tombstoned'
              AND c.generation = j.generation
       ))
   );
COMMIT;
```

## Ledger mapping

| Design name (`NATIVE_ENGRAM_DESIGN.md`) | This crate |
| --- | --- |
| `schema_migrations` | `engram_ingest.schema_migrations` |
| `source_records` | `engram_ingest.sources` |
| `source_units` | `engram_ingest.documents` + `engram_ingest.chunks` |
| `ingest_jobs` | `engram_ingest.jobs` + `engram_ingest.leases` |
| issue poll state | `engram_ingest.issue_cursors` |
| `transcript_watermarks` | `engram_ingest.transcript_watermarks` + `transcript_windows` |
| `gateway_events` | deferred to that slice |

`migrate()` bootstraps `engram_ingest.schema_migrations`, applies every embedded
migration version once in order under an advisory lock, and then verifies the
columns, constraints, and partial index the code depends on. It refuses a
database that records a version the binary does not embed. Migration `0002`
adds `jobs.last_error_class`; migration `0004` replaces the per-source lease
index with `jobs_one_leased_per_bank_document_idx` and requeues surplus leased
jobs before building it; migration `0005` adds `sources.source_kind`,
`issue_cursors`, and the `issue_removed` tombstone reason; migration `0006`
widens `source_kind` for discussions and boards. The v1→v2 and v2→v4 upgrades
are exercised by the Postgres harnesses.

Source identity is the unique `(project, tag, root)`. Issue, discussion, and
board streams set `tag = root` to a provider scope plus a config hash so a JQL,
prefix, repo, or project change cannot silently alias an existing source row.
Chunks are scoped by `source_id` with unique `(source_id, hindsight_document_id)`
and `(document_id, chunk_key)`, so two projects with the same tag/path can share
a Hindsight document id without touching each other's rows. That is the design's
`(project, source identity, unit key)` constraint. Streams in the same bank must
still use disjoint document namespaces; their Hindsight ids are global to the
bank.

`migrate()` also runs an unversioned bank guard on every call. Migration `0003`
adds nullable `sources.bank_id` / `jobs.bank_id` and the partial
`jobs_bank_pending_idx`. If an existing source has no bank, migration fails with
`MigrationGuard` telling the operator to set `sources.bank_id` explicitly in
their own connection; rerunning then backfills jobs, enforces `NOT NULL`, and
verifies the CHECK constraints and index.

## Docs shadow gate

`fixtures/docs_shadow_run.py` freezes the real `docs/` tree at a git revision,
runs the deployed CocoIndex `docs` app twice (unmodified and a deterministic
mutation that removes one real file and one real heading section), and compares
the captured retain payloads with the native planner. The oracle runs
`python -m engram.flows.configured --apps docs --mode backfill` against a
loopback Hindsight capture server, so timestamps, source identity, and the real
`docs_main`/`localfs` path are exercised rather than a synthetic fixture.

```bash
~/.engram/venv/bin/python \
  rust/crates/engram-ingest/fixtures/docs_shadow_run.py --rev origin/main
```

It writes `docs/findings/214-docs-shadow-gate.{md,json}` and exits non-zero on
an unexpected divergence. The checked-in report is the measured result, not an
assertion: on `2fe0bce` (37 files, 1,376,464 bytes) the oracle and the native
planner matched all 2,709 retains and all 12 oracle-derived deletes across the
mutation, with zero unexpected divergences. Ten document ids per snapshot are
repeated headings; the ledger intentionally keeps the last occurrence, which
the report records as `duplicate_collapse`. Duplicates spanning files, scan
conflicts, unattributed ids, and repeated bare keys fail the gate.

Path/chunk-key attribution uses the flow's own `_document_base_id` helper, so
it corroborates the emitted document ids rather than independently proving file
association. A non-default Postgres endpoint is passed with
`ENGRAM_SHADOW_PG_DSN` or `--pg-dsn`.

The oracle script, driver, and `docs_shadow` example are developer tooling.
Nothing in the service path imports them, and the Rust comparison core in
`src/shadow.rs` is covered by Rust unit tests. The checked-in report records
the exact deployed CocoIndex version; it does not claim parity for a different
pin (this repository pins `cocoindex==1.2.0`).

## Running the runtime

The crate now builds the `engram-ingest` binary. It reads the deployment
`projects.toml` subset already used by the Python flow:

```toml
[defaults]
hindsight_url = "http://localhost:8888"          # HINDSIGHT_URL wins
pg_dsn = "postgresql://hindsight@localhost/hindsight"  # COCOINDEX_PG_URL wins
scan_interval_seconds = 300                      # 1..=86400
concurrency = 1                                  # worker slots per bank, 1..=8
allow_empty_root = false                         # true opts into deleting on an empty root

[projects.example]
docs_bank = "example-docs"                       # or a unique banks entry ending -docs

# Optional transcript ingestion (issue #254), parsed exactly like
# src/engram/project_config.py: `transcripts` (legacy `transcript_enabled`)
# enables it; `transcript_bank` (legacy `transcripts_bank`) is shape-checked
# whenever set, even when disabled; the directory comes from
# `paths.transcripts_dir` (`[defaults.paths]` provides a fallback), expanded
# and resolved like a source root; `workspace_prefixes` selects the
# `{prefix}*/agent-transcripts/**/*.jsonl` workspaces under it. These keys
# must stay in the [projects.example] table, before any
# [[projects.example.sources]] entry.
transcripts = true
transcript_bank = "example-transcripts"
workspace_prefixes = ["Users-jgil-src-example-"]

[projects.example.paths]
transcripts_dir = "~/.cursor/projects"

[[projects.example.sources]]
tag = "example"
root = "~/src/example"                           # ~ and $VARS expand; relative to this file
docs_include = ["**/*.md"]
docs_exclude = ["**/vendor/**"]
document_format = "markdown"                     # "pdf" is rejected in this slice
```

Start the resident loop (SIGINT/SIGTERM cancel it, then the worker drains and
joins):

```bash
ENGRAM_PROJECTS_CONFIG=~/.engram/projects.toml \
  cargo run --manifest-path rust/Cargo.toml -p engram-ingest -- run --project example
```

One-shot scan + drain for a backfill or a harness:

```bash
cargo run --manifest-path rust/Cargo.toml -p engram-ingest -- \
  run --project example --once --drain-timeout 120
```

`--project` is repeatable. `--config` overrides `ENGRAM_PROJECTS_CONFIG`, which
overrides `~/.engram/projects.toml`. `--scan-interval` and `--concurrency`
override the file. Startup runs `migrate()` before any scan, so schema and
bank-guard failures stop the process instead of stranding jobs.

A missing or unreadable source root is skipped with a warning and the existing
manifest is left untouched. An existing root that produces zero files while the
manifest still has active documents is reported as `guarded`, because an
unmounted bind mount looks exactly like a real deletion; pass
`--allow-empty-root` (or `allow_empty_root = true`) to opt into that deletion.
Resident mode logs skips/guards and keeps serving; `--once` exits non-zero when
any source was skipped or guarded, so a mounted-root typo or withheld deletion
cannot look like a clean backfill. A moved or renamed root is a new source
identity (`project, tag, root`), so it starts a fresh ledger row and does
**not** migrate the old documents; keep roots stable or run an explicit
migration.

### Transcript sources in the runtime (issue #254)

A project with `transcripts = true` gets one transcript source with the fixed
ledger tag `transcripts` and the identity `(project, "transcripts",
paths.transcripts_dir)`; a project with no docs sources is valid as long as
transcripts are configured. The startup and periodic scans run
`ingest_transcript_source_with_discovery` after the docs sources with the
deterministic defaults (`RegexSignalDetector`, `RetainResolver`, Shadow
contradiction mode) — no LLM calls or dependencies. The transcript bank joins
`bank_ids()`, so the same bank-scoped worker supervisor serves its retains and
tombstones, and `--once` drains them with everything else.

Transcript-specific runtime semantics:

- A `paths.transcripts_dir` whose directory does not exist is `skipped` like
  a missing docs root: resident mode logs and continues (Python's
  `transcript_main` warns and returns); `--once` exits non-zero. A missing
  config *key* is a load error, not a skip.
- A transcript root that exists while discovery matches no files and the
  manifest still has active documents is `guarded` exactly like an empty docs
  root — a `workspace_prefixes` typo must not mass-tombstone retained windows.
  `--allow-empty-root` opts into treating it as a real deletion. The guard and
  the applied plan evaluate the same discovery snapshot.
- `--once` also exits non-zero when a transcript file was skipped
  (unreadable, invalid UTF-8, or over the 64 MiB read cap), so withheld data
  cannot look like a clean backfill; the startup label names each skipped
  file and reason (bounded to five, the count carries the full tally). An
  incomplete discovery walk (an unreadable nested directory or entry)
  likewise fails `--once` — removals are suppressed for that pass, but the
  transcripts in the unreadable subtree are missing from it. Windows
  withheld by a cross-path ownership conflict are reported in the outcome
  label as `conflicted`; the owning path retains that content, so conflicts
  are an observability signal that self-heals, not data loss, and do not
  fail `--once`. `Deferred` (`StaleReconciliation` from a concurrent
  runtime) and `Skipped`/`Guarded` follow the docs contract.
- Two projects may share one `transcript_bank`, matching Python: window ids
  are stem- and content-digest scoped, so a same-stem/same-text window in a
  shared bank converges on identical content instead of corrupting state.
  The delete side is not benign, though: when one project's file leaves
  scope, its tombstone removes the *shared* Hindsight document while the
  other project's ledger row stays active, and that project will not
  re-retain an already-owned window id — prefer one transcript bank per
  project when deletions matter. Same-project docs sources may not use the
  tag `transcripts` on the same root (that would alias the ledger identity
  and flip `source_kind`). Bank ids are additionally capped at 128
  characters (the ledger/Hindsight bound; Python's `_BANK_RE` has no length
  cap).
- The Python JSON watermark file (`transcript_watermarks`, default
  `~/.engram/logs/cocoindex-transcript-watermarks.json`) is not migrated:
  Postgres watermarks start empty and the first scan replays the same stable
  window ids, which re-retains identical content and converges without
  duplicates.
- Transcript scans honor shutdown between sources but not inside one source's
  discovery walk; the walk is bounded by the configured root and the 64 MiB
  per-file read cap.

`--once` waits for every pending job (including backoff) and every leased job
(even one whose lease expired but recovery has not reached), then exits 0. It
exits non-zero on a drain timeout, a cancelled drain that still has work, when
a source was skipped/guarded as described above, or when any job is marked
`dead` from the start of the run (including startup recovery).

`RuntimeHandle` exposes `shutdown()`, `health()`, `snapshot()`, `metrics()`
(Prometheus text), `startup_recovery()`, and `prepared()`. The binary prints the
startup reconciliation summary and the Prometheus snapshot on exit.

Supported targets are Unix (Linux/macOS). Non-Unix builds fail explicitly
because the scanner requires handle-based `openat`/`fdopendir` traversal to keep
the configured-root containment guarantee.

## Running the Postgres harness

The harness needs a disposable database whose name contains `test`:

```bash
createdb -h /tmp -U hindsight engram_ingest_test
ENGRAM_INGEST_TEST_DATABASE_URL='postgresql://hindsight@/engram_ingest_test?host=/tmp' \
  cargo test -p engram-ingest --test ledger_postgres --test sink_postgres \
             --test worker_postgres --test issues_postgres --test discussions_postgres
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

Unit-tier business-logic coverage (excludes the HTTP/Postgres adapters
`http`, `runtime`, `signals`, `store`, `sink`, `worker`, and the transcript
source adapter `transcript_ingest`; the transcript serializer/policy itself is
in `transcript.rs` and `transcripts_postgres` covers the adapter):

```bash
cargo llvm-cov -p engram-ingest --lib \
  --ignore-filename-regex '(http|runtime|signals|store|sink|worker|transcript_ingest)\.rs' \
  --fail-under-lines 90
```
