# CocoIndex deprecation inventory (issue #214)

Status: **code search is retired; ingestion still runs on CocoIndex.** This
document is the contract for removing the rest. Issue #214 closes only when
every behavior below has a Rust owner or is explicitly dropped.

## What CocoIndex is

CocoIndex is the Python incremental dataflow engine used by
`engram-flows-configured` (`src/engram/flows/configured.py`). It supplies:

- file watching and polling;
- memoized per-file/issue processing;
- per-project flow state in LMDB environments, usually
  `<project>-cocoindex.db` (kubernaut uses `cocoindex.db`), each containing
  `mdb/data.mdb` and `mdb/lock.mdb`;
- Postgres table targets and `cocoindex.*` schema bookkeeping.

Engram owns the source parsing, chunking, synthesis, and Hindsight calls;
CocoIndex owns the incremental flow engine around them.

## Live footprint

Deployment snapshot captured 2026-11-24 from `launchctl list` and the deployed
`~/Library/LaunchAgents` plists, with `lsof` port checks and
`psql`/`du` for state. Re-verify before acting on the retirement gates.

### Ingestion services (must stay until replaced)

These are the deployed `--apps` values. The repository plists were aligned to
them in this change; `code` is no longer passed by any flow plist.

| LaunchAgent | Project | `--apps` |
| --- | --- | --- |
| `io.vectorize.cocoindex.service` | kubernaut | `docs` `issues` |
| `io.vectorize.cocoindex.engram` | engram | `docs` |
| `io.vectorize.cocoindex.kuadrant` | kuadrant | `docs` `issues` `git-sync` |
| `io.vectorize.cocoindex.praxis` | praxis | `docs` `issues` |
| `io.vectorize.cocoindex.project-lightpath` | project-lightpath | `docs` `issues` |

`launchd/io.vectorize.cocoindex.configured.plist` is the template for new
projects and now passes `--apps docs issues`, so a new deployment does not
start the legacy code flow by default.

Support jobs:

- `io.vectorize.cocoindex.watchdog` re-bootstraps the continuously expected
  flow jobs after a silent `launchctl` unload. Its list covers four of the five
  services (project-lightpath is not listed).
- `io.vectorize.cocoindex.watch-sync` refreshes branch-scoped mirror worktrees
  every ~10 minutes (`refresh-watch-mirrors.sh`), keeping watched roots at the
  intended branch.

Entrypoint: `engram-flows-configured --project <project> --config
~/.engram/projects.toml --mode live --apps ...`.

The CLI still accepts `docs`, `issues`, `code`, `transcripts`, and `git-sync`.
The `code` app is still built when a project sets `code_table`, so it remains a
legacy backfill path even though no live service selects it. Omitting `--apps`
still selects every built app, including `code`, and auto-adds `git-sync` when
configured; that is the remaining way to start code ingestion without editing a
plist. It must be deleted with the rest.

### Code-search services (retired)

The five deployed `io.vectorize.cocoindex-code.*` jobs
(`engram-search-configured`) were booted out and disabled on 2026-11-24.
Ports `8891`, `8905`, `8906`, `8907`, and `8908` are closed. The old
`io.vectorize.cocoindex.search` template (`:8890`) was never loaded in this
deployment and is deleted with this change. zvec-grep on `:7999` owns semantic
search, graph, and managed rg. It does **not** own structural pattern search:
`engram_code_pattern_search` (Python `src/engram/search/engram.py`) has no
replacement, and its backend is disabled, so that capability is currently
lost. Serena's `search_for_pattern` covers literal/regex searches only on
routes that mount Serena. This is an explicit drop unless a Rust owner is
chosen. `src/engram/search/*` is shipped as the standalone
`engram-search-configured` server and entry point; the Rust gateway does not
mount it, but the legacy Python registry can. `code_overlay*` is not dead: it
is a runnable branch-overlay writer (see below). Both must be deleted or
re-pointed with the rest.

### Flow functions

`src/engram/flows/configured.py` defines these memoized/flow functions:

- `process_doc_file`, `process_pdf_file`, `docs_main`
- `process_code_file`, `code_main` (legacy code path)
- `process_issue`, `process_jira_issue`, `process_discussion`,
  `process_board`, `issues_main`
- `process_transcript`, `transcript_main`
- `process_overlay_file`, `overlay_main` (`src/engram/code_overlay_cocoindex.py`;
  branch-overlay code writer into `cocoindex.*` and
  `~/.engram/cocoindex-overlay.db` when a branch overlay is materialized)

Startup waits for Hindsight (`_wait_for_hindsight`: 30 attempts, 1.5x backoff
capped at 30s) and proceeds even if it never becomes ready.

## Behaviors to preserve

### Docs

- Walk configured roots with include/exclude globs; Markdown and PDF.
- Stable base ID:
  `"{effective_source_tag}--{relative path without extension, / -> --}"`.
- Heading-anchored chunks (`chunking.split_markdown_sections`, 800/200):
  - the preamble, or the first heading when there is no non-empty preamble,
    carries key `""`;
  - every later heading key is a 16-char sha1 of its own heading line;
  - an oversized section splits into `-part{n}` scoped to that section.
  - Document IDs are exactly `{base_id}`, `{base_id}--{key}`,
    `{base_id}--{key}-part{n}`, or `{base_id}--part{n}` when the key is empty.
  - The invariant is "stable from the second heading onward"; inserting a new
    first heading changes which section carries the bare ID.
  - A document with no headings (common for PDF-extracted text) falls back to
    positional keys: `""` then `chunk{i}`, so those chunks are **not** stable
    across edits.
- Metadata: `source=cocoindex`, `repo`, `key_sentences`, `keywords`; tags: the
  literal `root` for a top-level file or the first path part, plus source tag;
  timestamp from file mtime.
- Sink: Hindsight retain API, `<project>-docs` bank.

### Synthesis

`synthesize_document` produces `key_sentences` (TF-IDF/PageRank, budget
`min(12, max(5, len // 8000))`) and `keywords` (top terms). These affect recall
quality; Rust must reproduce them or an approved replacement.

### Issues

Stable IDs differ per provider:

- GitHub: `[{prefix}-]{short_repo}-{kind}-{number}`.
- Jira: `{prefix}-{key}`, prefix default `jira`.
- Discussion: `[{prefix}-]{short_repo}-discussion-{number}`.
- Board: `{prefix}-{number}-{slug}`, prefix default `board`.

`split_issue_sections` gives the header key `""`, comments `comment{i}`, and
oversized parts `part{n}` / `comment{i}-part{n}`; the document ID appends the
key as `-{key}`. Keys are stable for append-only, unfiltered prefixes. They are
**not** stable when the filtered comment list changes (author becomes
untrusted, body drops below 20 chars, or the 10-comment cap evicts an older
comment), because later ordinals shift. The Rust owner should key by a stable
comment id instead.

Metadata and tags are provider-specific:

- GitHub: metadata `source`, `repo`, `kind`, `number`, `state`, optional
  `milestone` and `milestone_due`; tags state, kind, short repo, up to 5
  labels, and `milestone:<title>`.
- Jira: metadata `source`, `tracker=jira`, `key`, `status`, `project`; tags
  status, issue type, `jira`, up to 5 labels, optional parent key.
- Discussion: metadata `source`, `repo`, `kind=discussion`, `number`,
  `category`; tags `discussion`, short repo, category slug.
- Board: metadata `source`, `kind=board`, `board_number`, `board_title`; tags
  `board`, `roadmap`, slug.

Sink: Hindsight retain API, `<project>-issues` bank.

### Transcripts

- Opt-in only (`--apps transcripts`), never unattended.
- Watermark file `~/.engram/logs/cocoindex-transcript-watermarks.json` stores
  `message_count` and `last_processed` per transcript.
- Learning windows contain up to 5 filtered entries: the signal plus two
  parsed messages before and after (empty/boilerplate messages are dropped, so
  the raw message span can be larger); window ID is
  `transcript-{transcript_id}-w{sha1(window)[:16]}`.
- Correction windows call `contradiction_resolution.resolve`:
  - `queued` skips the retain entirely and appends a pending-review entry;
  - `auto_resolved` adds `CORRECTION` and `supersedes-prior-memory` tags and
    writes the auto-resolved audit log. Only
    `ENGRAM_CONTRADICTION_AUTO_MODE=live` also PATCH-invalidates the
    superseded memory unit; the default `shadow` mode logs
    `invalidated=false` and changes no memory state;
  - `retain` (no contradiction or check disabled) retains normally.
- Sink: `shared-memory`.

### Incremental engine and failure modes

- Memoized per-file processing skips unchanged content.
- Retain calls make three attempts total (two retries, 1s then 2s backoff).
- A final retain failure is swallowed; transcript watermarks still advance, so
  failed windows are an at-most-once silent skip today. The Rust ledger must
  improve this without duplicating on retry.
- Live mode watches files and polls issue providers.
- There is no Hindsight `DELETE` in `configured.py`. Transcript corrections
  PATCH-invalidate a memory unit only in `ENGRAM_CONTRADICTION_AUTO_MODE=live`;
  the default `shadow` mode only logs. `delete_document` exists but is not
  reached from the flow. Source-document deletion is therefore a new gate, not
  existing behavior.

### Git sync

- Live only for kuadrant (`--apps docs issues git-sync`).
- `_git_pull_one` runs `git pull --ff-only --quiet` (120s timeout) on every
  deduplicated source root that has a `.git` directory, every
  `git_sync_seconds` (kuadrant: 21600). `--mode backfill` performs one pass.
- Retiring the Python service without a Rust owner silently stops repo sync.

### Hindsight boundary

Hindsight remains the store and embedder for docs, issues, and transcripts;
CocoIndex calls `/v1/default/banks/<bank>/memories`. The retired code flow was
the only path that embedded locally into pgvector.

## Data and state

Live flow-state LMDB environments:

| Store | Content | Current size |
| --- | --- | --- |
| `~/.engram/cocoindex.db` | kubernaut flow state | 364 MB |
| `~/.engram/praxis-cocoindex.db` | praxis flow state | 192 MB |
| `~/.engram/project-lightpath-cocoindex.db` | project-lightpath flow state | 108 MB |
| `~/.engram/kuadrant-cocoindex.db` | kuadrant flow state | 58 MB |
| `~/.engram/engram-cocoindex.db` | engram flow state | 23 MB |

On-demand/backfill-only state (no live service): `dcm-cocoindex.db` 388 MB,
`koku-cocoindex.db` 42 MB, `rhdh-plugins-cocoindex.db` 216 KB. Optional
branch-overlay state: `~/.engram/cocoindex-overlay.db` (currently absent).
Transcript watermarks: `~/.engram/logs/cocoindex-transcript-watermarks.json`.

Postgres `cocoindex.*` holds only legacy code embeddings and the call-graph
cache: ~1.9 GB total. `code_embeddings` 612 MB / 67,307 rows;
`praxis_code_embeddings` 304 MB; `kuadrant_code_embeddings` 82 MB;
`engram_code_embeddings` 34 MB; `project_lightpath_code_embeddings` 81 MB;
`dcm_code_embeddings` 469 MB; several empty shadow/overlay tables and
`call_graph_cache` remain. No live client reads them.

## What deprecation means

1. Build `engram-ingest` in Rust: Postgres manifests, leases, jobs, retries,
   tombstones, startup recovery; port docs chunking/synthesis/IDs, issue
   clients, and transcript watermarks/correction handling.
2. Prove parity on a frozen fixture: emitted operations, stable IDs, chunk
   text, and metadata; then shadow-run against the live Python flows.
3. Switch the five ingestion services to Rust and keep Python flows as the
   rollback for one soak window.
4. Disable `io.vectorize.cocoindex.*`, watchdog, and watch-sync; stop running
   `engram-flows-configured` and `engram-search-configured`.
5. Remove the Python flow/search code, entry points, tests, CocoIndex
   dependency, launchd/systemd templates, and docs. Archive or drop the
   `cocoindex.*` tables and LMDB state only after a backup (`mdb_copy` with the
   writer stopped) and a documented backout step.
6. Keep Hindsight data, Postgres, and zvec-grep untouched.

## Removal gates

- No enabled service runs `engram-flows-configured` or
  `engram-search-configured`.
- No flow plist passes `--apps code`, and `build_apps` no longer constructs the
  code app.
- No runtime import of `cocoindex`; `pyproject.toml` no longer depends on it.
- No reader or writer of `cocoindex.*` tables.
- Docs, issues, and transcript paragraphs reach Hindsight with the same stable
  IDs on a replay fixture; edits rewrite only affected chunks. Heading-less
  docs/PDFs are the known exception until the Rust owner defines a stable
  replacement key.
- Source deletion removes the corresponding Hindsight documents (new
  tombstone behavior, with tests).
- Transcript watermarks survive restart and do not duplicate windows.
- Rust ledger handles a crash mid-retain without duplication or silent loss.
- Code-search leftovers (`engram-search-configured`, `src/engram/search/*`,
  `code_overlay*`, call-graph cache, and the code-search plist/systemd unit)
  are deleted or re-pointed to zvec.
- Structural pattern search either has a Rust owner or is recorded as an
  accepted drop, and the legacy `engram_code_pattern_search` registration is
  removed.
- Git sync either has a Rust owner or is recorded as an accepted drop for
  kuadrant.
- Rollback to the last Python artifact is documented and tested, or the window
  is explicitly closed.

## Open risks

- No source-document delete exists today; the replacement intentionally adds
  it, while preserving transcript invalidate-only-in-live semantics.
- Retain failure is currently silent and watermarks advance anyway.
- Heading-less docs/PDFs use positional `chunk{i}` keys and will renumber on
  edits; the Rust owner must either accept that or define a stable key.
- Structural pattern search and kuadrant git-sync currently have no Rust owner;
  both must be implemented or explicitly dropped before the removal gate.
- CocoIndex LMDB state is an undocumented memoization format; the Rust ledger
  needs its own manifest, not a byte-compatible port.
- PDF extraction, Jira ADF, GitHub pagination/rate limits, discussion/board
  providers, and the transcript contradiction pipeline are parity surface.
- Watch mirrors are deployment-local; the Rust watcher needs explicit roots and
  branch handling.
- Hindsight retain is not idempotent by itself; stable IDs must be validated
  against duplicate consolidation.
