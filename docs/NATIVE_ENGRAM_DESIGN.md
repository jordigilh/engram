# Native Engram Runtime and CocoIndex Replacement

**Status:** Design proposal — implementation intentionally not started

**Date:** 2026-10-02

**Decision requested:** approve the scope, contracts, and migration gates before
adding the native runtime.

## 1. Purpose

Engram currently combines a Python gateway, several Python/CocoIndex flow and
search processes, a Python maintenance/runtime surface, Hindsight, Postgres,
zvec-grep, and Serena. This creates unnecessary resident processes and makes
the code-index path depend on CocoIndex even though zvec-grep now provides the
preferred semantic search and call-graph backend.

The target is a native Rust Engram runtime that removes the CocoIndex runtime
dependency while preserving the user-visible behavior that matters:

```text
OpenCode/OpenChamber
        |
        v
  engram (Rust)
    |       |       \
    |       |        \-- Serena (exact LSP code intelligence)
    |       \-- zvec-grep (semantic search and code graph)
    v
  Hindsight (memory API)
    |
    v
  Postgres (Hindsight persistence and native Engram state)
```

The migration is deliberately staged:

1. **Replace the stable Engram/Hindsight proxy with Rust.**
2. **Move the remaining CocoIndex-dependent ingestion, indexing, and gateway
   behavior into the Rust Engram runtime, then retire CocoIndex.**

This document describes the complete target so Stage 1 does not accidentally
lock us into an incomplete Stage 2 architecture.

### Target dependency rule

The final resident stack has these service-level dependencies:

1. Engram (the native Rust binary and ingestion/gateway runtime);
2. zvec-grep (semantic code search and code graph);
3. Hindsight (memory API and memory/indexing authority);
4. Postgres (Hindsight persistence plus native Engram coordination state); and
5. Serena (exact LSP-backed symbols, references, diagnostics, and edits).

CocoIndex is the only service being retired. The Python/CocoIndex flow
processes are replaced in the resident ingestion path by Rust tasks in Engram.
GitHub ingestion will use direct REST/GraphQL calls through a Rust HTTP/API
client rather than spawning the `gh` CLI; `gh` may remain as a temporary parity
fallback during migration, but is not a final runtime dependency. Optional
Python maintenance tools may remain until a separate migration. Serena remains
an external MCP service and is not reimplemented by Engram.

## 2. Scope boundary

### In scope

- The resident Engram gateway and Hindsight blue/green proxy.
- Filesystem watching and incremental source reconciliation.
- Markdown/document chunking and stable document IDs.
- GitHub issue/PR and Jira polling, serialization, and idempotent retention.
- Transcript correction-window detection and watermarking.
- Code indexing/search orchestration and branch/worktree provenance.
- Structural pattern search currently supplied by CocoIndex `CodePattern`.
- Code-graph query compatibility where zvec-grep is not yet sufficient.
- MCP routing, tool filtering, backend health, freshness, and provenance.
- Operational configuration, migrations, health checks, metrics, and service
  lifecycle needed by the native runtime.

### Explicitly not in the first two stages

- Reimplementing Hindsight's memory, consolidation, knowledge graph, local
  embeddings, or reranking engine.
- Replacing Postgres.
- Replacing Serena's LSP-backed symbol resolution, diagnostics, or safe edits.
  Serena remains the exact-code authority.
- Reimplementing the optional LLM learning/reflect/triage algorithms in Rust.
  Existing maintenance commands can remain a later compatibility surface while
  the resident runtime is migrated.
- Running multiple semantic code engines for one route and implicitly merging
  their results.

The Rust runtime may call Hindsight, zvec-grep, and Serena over their existing
protocols. A later optimization may embed a stable zvec engine library, but the
initial contract must not depend on private zvec implementation details.

## 3. Current system inventory

### 3.1 Hindsight proxy and gateway

`src/engram/pipeline/hindsight_proxy.py` is a small raw TCP proxy. It:

- permanently owns the public Hindsight port, normally `0.0.0.0:8888`;
- reads `~/.engram/state/active-backend.port` for every new client connection;
- defaults to backend `127.0.0.1:18888` when the state file is unavailable or
  corrupt;
- connects to the selected blue/green Hindsight process with a five-second
  timeout;
- pumps bytes bidirectionally without interpreting HTTP or MCP;
- leaves existing connections on the old backend during a blue/green swap;
- closes only the affected client when a backend is unavailable.

`src/engram/pipeline/engram_gateway.py` is a separate Python MCP aggregator. It
currently:

- exposes one project-scoped MCP endpoint;
- aggregates Hindsight, code, RCA, and Serena backends;
- filters and renames tools per project;
- supports HTTP, stdio, and shadow HTTP adapters;
- retries/restarts selected stdio backends;
- can shadow zvec-grep with CocoIndex for comparison without delaying the
  primary response;
- adds response shaping, provenance, and gateway metrics.

Stage 1 replaces only the stable raw proxy. Stage 2 replaces the gateway and
the resident flow/search orchestration with the same external contracts.

### 3.2 CocoIndex functionality that must be replaced

CocoIndex currently supplies more than one feature. The replacement work is
the following matrix, not just a dependency deletion:

| Capability | Current implementation | Native owner after migration |
|---|---|---|
| File discovery | `localfs.walk_dir` + glob matchers | Rust source registry + `notify` watcher + reconciliation scan |
| Delta detection | CocoIndex memo/lineage state | Content hashes, source manifests, and an idempotent job ledger in Postgres |
| Markdown chunking | heading-aware `chunking.split_markdown_sections` | Rust stable-heading chunker with compatibility tests |
| Issue chunking | stable header/comment chunk keys | Rust issue serializer and stable chunk-key generator |
| Code chunking | tree-sitter-backed `RecursiveSplitter` | zvec-grep indexed entities where possible; native Rust splitter only where needed |
| Local embeddings for code | CocoIndex `SentenceTransformerEmbedder` | zvec-grep local model/runtime |
| Code dense + BM25 search | CocoIndex pgvector tables + RRF | zvec-grep search API and local index |
| Structural pattern search | CocoIndex `CodePattern`/`match_code` | Engram-owned Rust tree-sitter structural matcher |
| Call graph | Engram `callgraph.py` built on `match_code` | zvec-grep codegraph sidecar/API; native fallback only during migration |
| Graph cache | `cocoindex.call_graph_cache` | zvec-grep graph artifact/cache, or an Engram-owned Postgres cache if required |
| Docs sink | Python HTTP calls to Hindsight retain | Rust Hindsight client with retry/backoff and bounded concurrency |
| Issues source | `gh` CLI / GitHub APIs and Jira REST | Rust HTTP clients, conditional polling, pagination, and rate-limit handling |
| Transcript source | JSONL scan + correction-window regexes | Rust JSONL parser, append watermark, and stable window IDs |
| Code branch scope | flow-specific roots plus overlay helper | zvec-grep workspace identity and Engram Git provenance envelope |
| App lifecycle | CocoIndex `App`, `lifespan`, `update_blocking` | one Rust supervisor with per-source tasks and restart isolation |
| Progress/health | CocoIndex logs and ad hoc reports | structured status endpoint, metrics, and durable job state |

The table is the definition of the CocoIndex replacement scope. A feature is
not considered migrated because an equivalent demo exists; its behavior must
pass the compatibility and freshness gates in Section 11.

### 3.3 Existing source profiles

The current flows cover these source families:

- **Engram:** repository Markdown docs and Python source.
- **Kubernaut:** repository docs, Go/TypeScript source, GitHub issues/PRs, and
  Cursor transcripts; release-line and mirror selection is project-specific.
- **Koku:** repository docs/source plus GitHub and Jira-backed issue content.
- **Praxis:** multiple Rust/Go repositories, curated docs/PDF inputs, GitHub
  issues/PRs, and Jira where configured.
- **DCM:** multiple repository docs/source plus GitHub issues/PRs.
- **RHDH plugins:** docs/source plus Jira issue content; currently disabled in
  the gateway but retained as a supported historical configuration.
- **Kuadrant:** multi-repository prior-art docs/source and issue content,
  mounted as recall-only reference material.
- **Configured projects:** TOML-defined source roots, banks, code patterns,
  and GitHub/Jira settings.

The native runtime must use data-driven project configuration rather than add a
new Python module or compiled branch for each project.

## 4. Target runtime architecture

### 4.1 One Rust binary, explicit roles

The binary should have explicit subcommands or modes so the deployment can
start with only the proxy and later enable the resident runtime:

```text
engram proxy       stable raw TCP Hindsight proxy (:8888)
engram gateway     project-scoped MCP gateway (:8896/:8898)
engram run         gateway + watchers + pollers + native indexes
engram reconcile   one-shot source reconciliation/backfill
engram status      health, freshness, jobs, and provenance
```

`run` is the eventual always-on service. The subcommands share the same core
libraries and configuration, but Stage 1 must not require the full runtime to
run the proxy.

Suggested crate boundaries:

```text
rust/
  crates/engram-cli/          argument parsing and process entrypoint
  crates/engram-core/         IDs, config, manifests, source events, errors
  crates/engram-proxy/        stable TCP proxy and blue/green semantics
  crates/engram-gateway/      MCP aggregation, routing, and compatibility
  crates/engram-sources/      filesystem, Git, GitHub, Jira, transcript input
  crates/engram-sinks/        Hindsight and Postgres adapters
  crates/engram-code/         pattern search and zvec-grep integration
  crates/engram-runtime/      supervisor, queues, retries, health, metrics
```

The first implementation may collapse small crates, but the domain boundaries
should remain visible. In particular, source acquisition must not know how a
Hindsight memory is stored, and the gateway must not own ingestion state.

### 4.2 Process topology

The intended production topology is:

```text
OpenCode/OpenChamber --MCP HTTP--> engram gateway
                                      |
                                      +--> Hindsight MCP/HTTP API
                                      +--> zvec-grep MCP/engine API
                                      +--> Serena MCP (exact code operations)
                                      +--> Postgres (native job/source state)

Hindsight ---------------------------> Postgres
engram proxy :8888 ------------------> active Hindsight :18888/:18889
```

The gateway and proxy may eventually be modes of one resident process, but
they must retain independent listener ownership and failure domains until a
graceful shutdown/restart design proves that combining them cannot interrupt
the stable Hindsight connection.

### 4.3 Ownership rules

| Concern | Owner | Rule |
|---|---|---|
| Persistent memory and recall | Hindsight | Engram uses the public API; it does not write Hindsight tables directly |
| Postgres schema/data | Hindsight + Engram | Separate schema/table namespace and migrations; no CocoIndex tables in the final state |
| Code semantic index | zvec-grep | One selected workspace/index per route; no silent backend fusion |
| Exact symbols/edits | Serena | Engram routes the exact LSP-backed service; it does not approximate these answers |
| Source freshness | Engram | Watches/polls, records provenance, retries, and reports stale state |
| MCP catalog and route isolation | Engram | Clients see one project-scoped server |
| Blue/green backend selection | Rust proxy | State-file read is per new TCP connection |

## 5. Data and state model

### 5.1 Source identity

Every input is represented by a stable source identity:

```text
project
source_tag
source_kind       docs | issue | pull_request | jira | transcript | code
repository
relative_path or remote_key
branch/release_line (when applicable)
```

Each source snapshot records:

```text
content_hash
source_version     mtime/ETag/remote updated timestamp where available
observed_at
indexed_at
status             pending | indexing | ready | failed | deleted
error/retry_after
```

### 5.2 Stable document IDs

Existing Hindsight document IDs are part of the compatibility contract. The
native implementation must preserve them for unchanged source families:

- Markdown sections use the existing heading-derived stable key.
- Issue/PR/Jira comments use the existing comment ordinal/key convention.
- Transcript windows use the existing transcript ID plus content digest.
- Code remains in zvec-grep and does not use Hindsight document IDs.

The native implementation must never let insertion of a new document section
renumber unrelated sections. Deleted source units must be explicitly deleted
from Hindsight using its document API; a missing local source is not a reason
to silently leave stale memories behind.

### 5.3 Postgres state

Postgres is used for native Engram coordination, not as a replacement Hindsight
storage layer. The initial schema should be versioned and namespaced, for
example:

```text
engram.schema_migrations
engram.source_records
engram.source_units
engram.ingest_jobs
engram.transcript_watermarks
engram.gateway_events
```

Required properties:

- unique `(project, source identity, unit key)`;
- content hash and last successful sink operation;
- retry attempt, next retry time, and last error;
- lease/owner fields so two runtime instances cannot process one unit;
- tombstones for deleted source units until the Hindsight delete succeeds;
- no dependency on CocoIndex's SQLite state or `cocoindex.*` tables.

Code-index state and graph artifacts remain zvec-grep-owned. Engram stores only
the route/worktree/provenance record needed to decide whether a result is
fresh enough to serve.

## 6. Source and ingestion behavior

### 6.1 Filesystem sources

The Rust watcher must be a hint, not the source of truth:

1. Watch configured roots for create/modify/remove/rename events.
2. Debounce bursts and coalesce paths.
3. Re-read the current file before processing; never trust the event payload.
4. Hash and reconcile against `source_units`.
5. Queue only changed units.
6. Periodically run a bounded reconciliation scan to recover missed events.
7. On startup, resume pending jobs and scan for changes since the last
   successful checkpoint.

This is stronger than relying on a long-lived watcher alone and covers editor
atomic-save behavior, branch switches, sleep/wake, and dropped filesystem
events.

### 6.2 Hindsight retention

The Rust Hindsight client must provide:

- health/readiness probing;
- retain with stable `bank_id`, `document_id`, metadata, tags, and timestamp;
- document deletion for source tombstones;
- bounded concurrency per bank;
- exponential backoff with jitter for transient errors;
- request timeouts and cancellation;
- idempotent retry behavior;
- structured error classification;
- metrics for accepted, changed, deleted, retried, and failed units.

Hindsight is allowed to be temporarily unavailable. Source changes remain in
the durable job ledger and are retried; the watcher must not block indefinitely
or drop later changes behind one failed document.

### 6.3 GitHub and Jira

The native pollers must preserve current coverage and semantics:

- paginate rather than assume one API page;
- fetch all configured issue/PR states needed by the project;
- use ETags/updated timestamps where available;
- respect rate limits and retry-after values;
- serialize stable, non-volatile headers separately from comments;
- retain each issue/PR/ticket under its existing stable document ID;
- delete or tombstone records that leave the configured source scope;
- expose last poll, item count, cursor, and error in status;
- isolate one repository/provider failure from other project sources.

The initial provider implementation may invoke an authenticated `gh` command
for behavioral parity, but the final target must use direct GitHub REST/GraphQL
requests through a Rust HTTP/API client. This is not an attempt to reproduce
the entire `gh` CLI; the client only needs the issue, pull-request, comment,
review, pagination, conditional-request, and rate-limit operations required by
Engram. `octocrab` is the leading candidate: it provides typed Issues/Pulls
APIs, pagination, GraphQL, rate-limit endpoints, and a lower-level HTTP escape
hatch. It does not replace `gh`'s credential store or Engram's retry/ETag
policy, so those remain native runtime responsibilities. The migration must
define an explicit token source (`GITHUB_TOKEN` / `GH_TOKEN` or the deployment
credential store) before removing the `gh` fallback. Jira credentials must
likewise come from the existing deployment credential mechanism, not from
committed project configuration.

### 6.4 Transcripts

The transcript source is append-oriented JSONL:

- parse only complete lines;
- retain a per-file message-count/byte watermark;
- process only newly appended messages after restart;
- detect the same correction/instruction windows as the current flow;
- generate the same stable digest-based window document IDs;
- tolerate truncation, rotation, malformed lines, and concurrent writes;
- never duplicate already retained windows after a retry.

Transcript ingestion must remain independent from the optional LLM extraction
and reflection pipeline. It supplies raw learning windows to Hindsight; it does
not silently spend LLM tokens.

## 7. Code intelligence replacement

### 7.1 zvec-grep as the code backend

zvec-grep is the primary code backend. Engram should route code queries to it
with an Engram-owned provenance envelope containing at least:

```text
project
repository
worktree
branch
current_commit
indexed_commit
indexed_at
includes_uncommitted
stale_or_warning
```

The route must select one current workspace index. It must not query zvec-grep,
CocoIndex, and another code engine concurrently and merge results in
production. During migration, CocoIndex may run as an asynchronous shadow
only, with no effect on the primary response.

zvec-grep currently covers the target semantic retrieval and code-graph query
classes (hybrid search, managed lexical search, blast radius, shortest path,
and Leiden clustering in the current Rust codegraph work). Its index lifecycle,
workspace freshness, local embedding model, and graph artifact version must be
reported or wrapped by Engram rather than inferred from a successful query.

### 7.2 Structural pattern search (Engram-owned)

Structural by-example search is the notable remaining code feature not implied
by ordinary semantic search or the current zvec MCP catalog. It is not a
semantic retrieval service, so the target implementation belongs in Engram's
Rust code layer rather than in the zvec route.

The matcher must be an explicit tree-sitter implementation, not a text-regex
approximation. It must preserve the current `CodePattern` contract: metavariable
syntax, language detection, file filters, source ranges, rendered enclosing
context, Go/Python/Rust/TypeScript/TSX support, and configured multi-repository
scopes. Parse failures must be reported rather than treated as proof of no
match. Engram may share grammar/version fixtures with zvec-grep, but zvec owns
semantic retrieval and graph services while Engram owns this structural tool.

### 7.3 Call graph compatibility

The Rust runtime should use zvec-grep's codegraph API for:

- callers/blast radius by depth;
- shortest directed call path;
- function cluster and full community assignments;
- unresolved and ambiguous resolution counters;
- branch/worktree freshness.

Until zvec-grep's graph behavior passes the parity gate, the native runtime may
keep a migration-only comparison adapter. It must not expose two competing
answers under the same tool name or make CocoIndex a synchronous fallback.

### 7.4 Branch/worktree semantics

Each linked worktree has an independent active index. Engram owns the Git
provenance sidecar/record because a zvec manifest alone is not sufficient to
prove branch and commit identity. A changed branch, commit, root, index
version, or dirty-file digest must produce `stale_or_warning` and trigger an
asynchronous rebuild/reconciliation; it must not silently serve another
worktree's index as current.

## 8. Gateway and MCP contract

The Rust gateway must preserve the current client-facing properties:

- one project-scoped MCP HTTP endpoint;
- stable tool names and JSON schemas during migration;
- per-project backend allowlists;
- no duplicate direct Hindsight/CocoIndex/Serena registration required by the
  client during migration; the final client registers only Engram;
- per-backend degradation: one unavailable backend does not remove unrelated
  tools or crash the gateway;
- bounded forwarding timeouts and cancellation;
- correct MCP session initialization, notifications, calls, and DELETE
  handling;
- structured error responses that identify the backend without leaking
  credentials or local secrets;
- normalized Hindsight recall output and provenance where currently promised;
- shadow comparisons are asynchronous and bounded.

The gateway should model backends behind a typed trait:

```text
list_tools(project) -> tool catalog
call(project, tool, arguments) -> MCP result
health(project) -> health/freshness snapshot
```

Native source ingestion is not a gateway backend. It is a runtime task that
updates Hindsight and zvec state independently of a client's MCP request.

## 9. Reliability and resource design

### 9.1 Failure isolation

The supervisor must isolate:

- one source file or remote item failure;
- one project/provider failure;
- Hindsight unavailability;
- zvec indexing failure;
- a malformed provenance/state record;
- a dead optional Serena or RCA backend.

Only unrecoverable configuration or process-level faults should terminate the
resident runtime. launchd/systemd remains responsible for process restart.

### 9.2 Backpressure

Queues must be bounded. When a source changes repeatedly while a job is active,
coalesce to the newest content hash rather than enqueueing every filesystem
event. A slow Hindsight retain or index rebuild must not exhaust memory with
unbounded pending work.

### 9.3 Memory target

The reason for this rewrite is operational memory and process overhead, not the
assumption that Rust makes model inference free. The acceptance baseline must
measure separately:

- resident proxy RSS;
- gateway RSS;
- ingestion/watch runtime RSS;
- zvec-grep RSS and model cache;
- Hindsight RSS and Postgres RSS;
- cold start and steady-state CPU;
- queue depth and indexing latency.

Rust should avoid loading an embedding model in every project task. zvec-grep
must remain the single owner of the code embedding runtime for code search.

## 10. Migration stages

### Stage 0 — contract and baseline

- Freeze the tool/schema/source compatibility matrix.
- Capture current proxy/gateway/flow/zvec/Hindsight/Postgres RSS and latency.
- Record representative corpora and exact branch/worktree states.
- Add golden fixtures for document IDs, chunk boundaries, issue serialization,
  transcript windows, tool catalogs, and provenance envelopes.
- Keep current uncommitted zvec routing work as the migration pilot, not as a
  reason to delete the legacy path prematurely.

### Stage 1 — native stable proxy

- Implement `engram proxy` in Rust with no Python imports.
- Match the raw TCP, state-file, fallback, timeout, and connection-lifetime
  contract exactly.
- Add unit tests and a real loopback integration test with a fake backend.
- Add a launchd opt-in/rollback procedure.
- Run it alongside the Python proxy on an alternate listener, then switch the
  service only after the live Hindsight retain/recall and blue/green swap smoke
  tests pass.
- Do not remove the Python package or CocoIndex yet.

### Stage 2A — native gateway shell

- Port the gateway protocol adapter and route registry to Rust.
- Preserve the client-facing MCP schemas and per-project tool filtering.
- Route existing Hindsight and zvec/Serena HTTP/MCP backends through Rust.
- Keep Python gateway as a shadow/rollback path until catalog and error tests
  pass.

### Stage 2B — native document and transcript ingestion

- Implement Postgres migrations, source manifests, leases, jobs, and retries.
- Port file watching, reconciliation, stable Markdown/issue chunking, and
  Hindsight retention/deletion.
- Port transcript append watermarks and correction-window extraction.
- Run native and CocoIndex in shadow mode against a frozen fixture and compare
  emitted operations, not only final counts.

### Stage 2C — native remote issue ingestion

- Port GitHub and Jira clients/pollers.
- Validate pagination, rate limits, conditional requests, stable IDs, and
  out-of-scope deletion.
- Compare serialized content and source freshness against the current flow.

### Stage 2D — complete code feature parity

- Route all eligible project code search to zvec-grep.
- Finish pattern search through zvec or the native tree-sitter service.
- Retire `callgraph.py`/CocoIndex graph cache after zvec graph parity passes.
- Validate worktree/branch/release-line provenance and tombstones.

### Stage 2E — CocoIndex retirement

Only after all gates pass:

- stop and disable CocoIndex launchd/systemd jobs;
- remove `cocoindex` from Python packaging and lockfiles;
- remove CocoIndex flow/search adapters and direct `cocoindex.*` SQL;
- migrate or archive old code tables and flow state;
- remove CocoIndex-specific docs, watchdogs, overlays, and tests;
- update install/upgrade/rollback documentation;
- retain a documented data migration/backout procedure.

## 11. Acceptance gates

No stage may be declared complete solely because its unit tests pass.

### Proxy gate

- Persistent client connection survives a Hindsight blue/green swap.
- New connections use the new state-file port; existing connections remain on
  the old backend.
- Missing/corrupt state file uses the default backend.
- Backend timeout/refusal closes one client without taking down the listener.
- Loopback retain and recall through the Rust proxy match direct Hindsight.
- Stable listener restart/rollback is documented and tested.

### Gateway gate

- Tools/list and representative tools/call responses are schema-compatible.
- MCP initialization, session reuse, DELETE, and backend failure behavior pass
  protocol tests.
- Per-project catalogs match the approved registry.
- RSS and p95 latency are no worse than the Python gateway baseline; target is
  materially lower steady-state RSS.

### Ingestion gate

- Frozen fixture emits the same stable Hindsight document IDs and equivalent
  content for docs/issues/transcripts.
- Replaying the same source is idempotent.
- Editing one section/comment does not rewrite unrelated units.
- Source deletion removes the corresponding Hindsight document(s).
- A failed sink operation is retried after restart without duplication.
- Dropped watcher events are repaired by reconciliation.
- GitHub/Jira pagination and rate-limit cases are covered.

### Code gate

- zvec search and graph results carry correct current/indexed provenance.
- Branch switches, linked worktrees, staged/unstaged/untracked/deleted files,
  and stale indexes are detected.
- Semantic search quality is measured against the frozen CocoIndex baseline;
  graph callers/paths/clusters are measured against the approved graph fixture.
- Structural pattern search reaches parity or has an explicitly approved
  documented limitation before CocoIndex is removed.
- No synchronous shadow or hidden secondary code index remains in production.

### Retirement gate

- `grep`/dependency inspection shows no runtime import or subprocess path to
  CocoIndex.
- No enabled service references a CocoIndex flow/search command.
- No production query reads `cocoindex.*` tables.
- A fresh install requires only the approved runtime dependencies:
  **Engram, zvec-grep, Hindsight, Postgres, and Serena** (plus OS-level
  Git/provider credentials).
- Rollback to the last native/Python boundary is tested before deleting old
  state.

## 12. Open decisions before implementation

1. **zvec integration:** call zvec-grep over MCP/HTTP initially, or make its
   Rust engine a versioned library dependency of the Engram workspace?
2. **Tree-sitter grammar sharing:** pin the grammars independently in Engram,
   or expose a stable shared grammar/extraction package between Engram and
   zvec-grep without coupling either runtime to private internals?
3. **Postgres state schema:** one shared Engram schema for all projects versus
   per-project schemas/tables?
4. **Gateway/proxy process model:** one binary with separate modes first, or one
   process with both listeners from the first production rollout?
5. **Python maintenance horizon:** leave optional nightly/maintenance tools in
   Python after CocoIndex removal, or schedule a later Rust port?
6. **Data migration:** rebuild code indexes from current worktrees and retain
   existing Hindsight documents, or provide a full export/import verifier?
7. **GitHub client/auth:** validate `octocrab` against the required issue/PR
   fixtures and choose its credential source—environment variables, macOS
   keychain, or both.

The recommended defaults are: protocol integration first; Engram-owned pattern
search; `octocrab` plus an Engram-owned retry/ETag/rate-limit layer; one
`engram` Postgres schema; separate proxy/gateway modes in one binary; Serena
retained as the exact-code authority; Python maintenance temporarily retained;
and rebuild code indexes while preserving Hindsight document IDs.

## 13. Proposed first implementation slice

Before writing the native proxy or removing any Python dependency, add the
following reviewable artifacts:

1. a Rust workspace skeleton containing only the proxy crate;
2. contract tests derived from `tests/test_hindsight_proxy.py` plus a real
   loopback backend test;
3. a Stage 0 baseline report with RSS/latency and service topology;
4. a launchd opt-in plist and rollback instructions;
5. this document updated with the answers to the open decisions that affect
   crate boundaries or wire compatibility.

The proxy implementation should then be the first code change. CocoIndex
removal, source ingestion, and gateway replacement should not be mixed into
that change.
