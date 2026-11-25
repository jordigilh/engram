# CocoIndex deprecation contract (issue #214)

Status: **code search is retired; ingestion still runs on CocoIndex.** This
document defines what "retire CocoIndex" means. It is deployment-agnostic:
enumerate your own routes, services, and state paths with the checks below.

## What CocoIndex is

CocoIndex is the Python incremental dataflow engine used by
`engram-flows-configured` (`src/engram/flows/configured.py`). It supplies:

- file watching and polling;
- memoized per-file/issue processing;
- per-project flow state in LMDB environments, configured per project as
  `cocoindex_db` (each environment contains `mdb/data.mdb` and
  `mdb/lock.mdb`);
- Postgres table targets and `cocoindex.*` schema bookkeeping.

Engram owns the source parsing, chunking, synthesis, and Hindsight calls;
CocoIndex owns the incremental flow engine around them.

## Footprint to inventory

Do not hardcode one host's routes in this contract. For every deployment,
enumerate the flow services and record:

```bash
# macOS
launchctl list | awk '/engram|cocoindex/ {print $1, $3}'
# Linux
systemctl --user list-units 'engram-*' --all
```

For each ingestion service, record:

- project name and source roots;
- `--apps` selection (`docs`, `issues`, `code`, `transcripts`, `git-sync`);
- mode (`live` or `backfill`) and supervision/watchdog coverage;
- `cocoindex_db` LMDB path and any branch-overlay state;
- Postgres `cocoindex.*` tables it can write.

Run `engram-flows-configured --help` for the supported app list. Omitting
`--apps` selects every built app, including the legacy `code` app, and
auto-adds `git-sync` when configured. Service templates should list apps
explicitly; a project with `git_sync = true` must add `git-sync` itself.

## Code-search services (retired)

Code search is owned by zvec-grep: semantic search, graph, and managed rg.
The old `engram-search-configured` code-search services and their
launchd/systemd templates are retired. CocoIndex no longer writes or serves a
code index for clients.

Known gaps:

- Structural pattern search (`engram_code_pattern_search`) has no zvec
  replacement. Serena's `search_for_pattern` covers literal/regex searches
  only on routes that mount Serena. This is an explicit drop unless a Rust
  owner is chosen.
- `src/engram/search/*` is shipped as the standalone
  `engram-search-configured` server and entry point; the Rust gateway does not
  mount it, but a legacy Python registry can.
- `code_overlay*` is a runnable branch-overlay writer into `cocoindex.*`, not
  dead code. It must be deleted or re-pointed with the rest.

### Flow functions

`src/engram/flows/configured.py` defines:

- `process_doc_file`, `process_pdf_file`, `docs_main`
- `process_code_file`, `code_main` (legacy code path)
- `process_issue`, `process_jira_issue`, `process_discussion`,
  `process_board`, `issues_main`
- `process_transcript`, `transcript_main`
- `process_overlay_file`, `overlay_main` (`src/engram/code_overlay_cocoindex.py`)

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
  - an oversized section splits into `-part{n}` scoped to that section;
  - document IDs are exactly `{base_id}`, `{base_id}--{key}`,
    `{base_id}--{key}-part{n}`, or `{base_id}--part{n}` when the key is empty;
  - stability holds from the second heading onward; a new first heading
    changes which section carries the bare ID;
  - heading-less docs (common for PDF text) fall back to positional keys
    `""` then `chunk{i}` and are **not** stable across edits.
- Metadata: `source=cocoindex`, `repo`, `key_sentences`, `keywords`; tags: the
  literal `root` for a top-level file or the first path part, plus source tag;
  timestamp from file mtime.
- Sink: Hindsight retain API.

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

Sink: Hindsight retain API.

### Transcripts

- Opt-in only (`--apps transcripts`), never unattended.
- Watermark file stores `message_count` and `last_processed` per transcript.
- Learning windows contain up to 5 filtered entries: the signal plus two
  parsed messages before and after (empty/boilerplate messages are dropped, so
  the raw span can be larger); window ID is
  `transcript-{transcript_id}-w{sha1(window)[:16]}`.
- Correction windows call `contradiction_resolution.resolve`:
  - `queued` skips the retain entirely and appends a pending-review entry;
  - `auto_resolved` adds `CORRECTION` and `supersedes-prior-memory` tags and
    writes the audit log. Only `ENGRAM_CONTRADICTION_AUTO_MODE=live` also
    PATCH-invalidates the superseded memory unit; default `shadow` logs
    `invalidated=false` and changes no memory state;
  - `retain` retains normally.
- Sink: the configured transcript bank (default `shared-memory`).

### Incremental engine and failure modes

- Memoized per-file processing skips unchanged content.
- Retain calls make three attempts total (two retries, 1s then 2s backoff).
- A final retain failure is swallowed; transcript watermarks still advance, so
  failed windows are an at-most-once silent skip today. The Rust ledger must
  improve this without duplicating on retry.
- There is no Hindsight `DELETE` in `configured.py`. Transcript corrections
  PATCH-invalidate a memory unit only in `ENGRAM_CONTRADICTION_AUTO_MODE=live`;
  `delete_document` exists but is not reached from the flow. Source-document
  deletion is a new gate, not existing behavior.

### Git sync

- `_git_pull_one` runs `git pull --ff-only --quiet` (120s timeout) on every
  deduplicated source root that has a `.git` directory, every
  `git_sync_seconds`. `--mode backfill` performs one pass.
- Retiring the Python service without a Rust owner silently stops repo sync.

### Hindsight boundary

Hindsight remains the store and embedder for docs, issues, and transcripts.
The retired code flow was the only path that embedded locally into pgvector.

## Data and state

For every deployment, inventory:

- per-project LMDB environments named by `cocoindex_db` (do not assume a
  filename pattern; some deployments use `cocoindex.db`);
- optional branch-overlay state (`code_overlay_db`, default
  `~/.engram/cocoindex-overlay.db`);
- transcript watermarks (default
  `~/.engram/logs/cocoindex-transcript-watermarks.json`);
- Postgres `cocoindex.*` legacy code tables and the call-graph cache. No
  client should read them after code-search retirement.

Back up LMDB with `mdb_copy` while the writer is stopped, and record a tested
backout before dropping anything.

## What deprecation means

1. Build `engram-ingest` in Rust: Postgres manifests, leases, jobs, retries,
   tombstones, startup recovery; port docs chunking/synthesis/IDs, issue
   clients, and transcript watermarks/correction handling.
2. Prove parity on a frozen fixture: emitted operations, stable IDs, chunk
   text, and metadata; then shadow-run against the Python flows.
3. Switch ingestion services to Rust and keep Python flows as the rollback for
   one soak window.
4. Disable the flow, watchdog, and watch-sync services; stop running
   `engram-flows-configured` and `engram-search-configured`.
5. Remove the Python flow/search code, entry points, tests, CocoIndex
   dependency, launchd/systemd templates, and docs. Archive or drop
   `cocoindex.*` tables and LMDB state only after backup and backout testing.
6. Keep Hindsight data, Postgres, and zvec-grep untouched.

## Removal gates

- No enabled service runs `engram-flows-configured` or
  `engram-search-configured`.
- No flow service passes `--apps code`, and `build_apps` no longer constructs
  the code app.
- No runtime import of `cocoindex`; `pyproject.toml` no longer depends on it.
- No reader or writer of `cocoindex.*` tables.
- Docs, issues, and transcript paragraphs reach Hindsight with the same stable
  IDs on a replay fixture; edits rewrite only affected chunks. Heading-less
  docs/PDFs are the known exception until a stable replacement key exists.
- Source deletion removes the corresponding Hindsight documents (new tombstone
  behavior, with tests).
- Transcript watermarks survive restart and do not duplicate windows.
- Rust ledger handles a crash mid-retain without duplication or silent loss.
- Code-search leftovers (`engram-search-configured`, `src/engram/search/*`,
  `code_overlay*`, call-graph cache, and the code-search templates) are deleted
  or re-pointed to zvec.
- Structural pattern search either has a Rust owner or is recorded as an
  accepted drop, and the legacy `engram_code_pattern_search` registration is
  removed.
- Git sync either has a Rust owner or is recorded as an accepted drop.
- Rollback to the last Python artifact is documented and tested, or the window
  is explicitly closed.

## Open risks

- No source-document delete exists today; the replacement intentionally adds
  it, while preserving transcript invalidate-only-in-live semantics.
- Retain failure is currently silent and watermarks advance anyway.
- Heading-less docs/PDFs use positional `chunk{i}` keys and will renumber on
  edits; define a stable key or accept it.
- Structural pattern search and git-sync currently have no Rust owner; both
  must be implemented or explicitly dropped before the removal gate.
- CocoIndex LMDB state is an undocumented memoization format; the Rust ledger
  needs its own manifest, not a byte-compatible port.
- PDF extraction, Jira ADF, GitHub pagination/rate limits, discussion/board
  providers, and the transcript contradiction pipeline are parity surface.
- Hindsight retain is not idempotent by itself; stable IDs must be validated
  against duplicate consolidation.
