# Semantic Code Intelligence: Findings and Pilot Decision

**Status:** The local gateway integration now routes Kubernaut code tools to
zvec-grep as primary and schedules CocoIndex comparisons asynchronously for
semantic search and comparable Go core/operator graph calls. The native gateway
route is active for Kubernaut; quality gates remain open because this pilot is
exploratory and has no adjudicated relevance labels. Other Kubernaut-family
routes remain on the shared CocoIndex backend. zvec-grep exposes root-scoped,
fresh callgraph MCP tools for Go, Rust, TypeScript/TSX, and Python.

**Date:** 2026-09-22

**Related work:** [issue #107](https://github.com/jordigilh/engram/issues/107),
[Graphify-inspired call graphs](CALL_GRAPH_DESIGN.md), and the
[CocoIndex operations guide](COCOINDEX.md)

## Decision Summary

Engram's responsibility remains project watching and memory/document ingestion
into Hindsight. For the interactive live-worktree code profile, the local
integration direction is:

- zvec-grep is primary for semantic search and root-scoped callgraph operations
  over the selected current worktree.
- CocoIndex remains an asynchronous shadow comparator on the same configured
  Kubernaut source root; shadow latency or failure must never delay or alter the
  primary result.
- Serena and the existing CodeGraph server remain the authority for exact,
  type-resolved references, implementations, diagnostics, and edits.
- Hindsight remains the source for retained project memory, documents, and
  issue context.

This is a local pilot, not a completed rollout. Do not retire CocoIndex or
change other family routes until same-corpus retrieval, branch freshness,
provenance, latency, and error-isolation gates pass. The historical
open-codebase-index and Codanna measurements below remain evidence, not current
production-routing decisions.

## Workload-Specific Backend Direction

The best code backend depends on whether the agent is working in a persistent
developer worktree or reviewing an isolated repository snapshot. These are
separate deployment profiles; Engram should not run multiple code indexes for
the same session and merge their results implicitly.

### Interactive worktree profile

For a live developer worktree, the direction remains the zvec-grep fork plus
Serena:

- zvec-grep provides hybrid semantic and lexical retrieval over the current
  branch and changed-file overlay.
- The overlay manager can account for staged, unstaged, untracked, deleted,
  and renamed files while retaining merge-base provenance.
- Serena remains authoritative for exact symbols, references,
  implementations, diagnostics, and edits.
- The backend must continue to support Engram's multi-repository, mixed-file,
  and branch/worktree freshness contracts.

This profile is the reason zvec-grep is being extended with the versioned
`codegraph-v1` sidecar described in issue
[#113](https://github.com/jordigilh/engram/issues/113). The goal is one
branch-aware code backend plus Serena, not concurrent zvec-grep, Sense, and
other code indexes.

### Disposable code-review profile

For code review, a backend can clone a repository at the pull request or target
commit, index that clone, answer review questions, and discard the clone and
its indexes afterward. In that workflow, Sense plus Serena is a stronger
candidate than the zvec-grep overlay design:

```text
clone the review revision
  -> Sense scan and graph/search queries
  -> Serena exact references, implementations, diagnostics, and edits
  -> discard the clone, Sense index, and Serena process
```

A single clone represents one branch or commit, so Sense does not need
Engram's base-plus-overlay federation, live dirty-file reconciliation, or
multi-worktree index sharing. Sense's semantic search, symbol relationships,
callers/callees, impact analysis, conventions, and diff-oriented blast
analysis are useful for review-oriented questions. Serena complements those
results with exact LSP-backed navigation and validation.

Sense is not an out-of-the-box replacement for every Engram code contract. A
review adapter would still need to provide lifecycle and provenance handling,
and Sense does not directly replace Engram's AST pattern search, Leiden
clustering, or broad mixed-file search over YAML, JSON, Rego, Tape, and other
operational artifacts. Therefore this profile is intended for code-focused
reviews, not as a replacement for the interactive worktree backend.

The resulting selection is:

| Workload | Backend direction |
| --- | --- |
| Live worktree with dirty files, branch overlays, mixed files, and multiple worktrees | zvec-grep fork plus Serena |
| Disposable, code-focused pull-request or repository review clone | Sense plus Serena |

The Sense profile should be evaluated as a separate backend mode. It should not
be introduced by running both code indexes for the same project and silently
combining results.

## Existing Baseline

Engram already has three distinct code-intelligence layers:

| Layer | Current capability | Authority |
|---|---|---|
| CocoIndex hybrid search | Dense plus BM25/RRF retrieval over AST-aware code chunks | Concept and identifier discovery |
| CocoIndex structural graph | Tree-sitter extraction, cross-file call graph, blast radius, shortest path, and Leiden clustering | Fast broad structural analysis |
| Serena | LSP-backed symbols, references, implementations, diagnostics, and safe edits | Exact type-resolved navigation and refactoring |

The Graphify-inspired changes are an Engram-local extension built on
CocoIndex's `match_code()` primitive. They are not a separate Graphify service.
The shared implementation lives in `src/engram/callgraph.py` and is exposed
through:

- `*_call_graph_blast_radius`
- `*_call_graph_shortest_path`
- `*_call_graph_get_cluster`

The graph builder is deliberately fast and conservative. It uses tree-sitter
extraction, name-based resolution, same-file preference, signature-compatible
candidate filtering, and an "exactly one candidate or no edge" policy. It does
not have compiler-level type resolution. Ambiguous and unresolved calls are
reported rather than guessed.

This makes the current graphify layer a useful structural baseline, but not a
replacement for Serena. It also means a new backend should not simply expose a
second copy of `blast_radius` and `shortest_path` without proving better
resolution or materially better scope and freshness behavior.

## Why `open-codebase-index` Is an Alternative to CocoIndex Search

The reason to evaluate `open-codebase-index` is primarily **branch and
worktree semantics**, not an assumption that its basic semantic retrieval is
better than CocoIndex's dense-plus-BM25/RRF search.

The relevant additional capabilities are:

- Branch-aware catalogs and content-hash reuse
- Worktree-specific index boundaries
- Incremental file watching
- Explicit index status and health reporting
- Definition lookup, call-graph paths, and pull-request impact queries
- Optional SCIP TypeScript enrichment

CocoIndex remains the default retrieval baseline because it is already
integrated, local, and operationally understood. Its code search should not be
replaced until a measured comparison shows a benefit. The pilot exists because
issue #107 needs code-search responses to identify the current worktree,
branch, commit, indexed revision, and whether uncommitted changes are
included. `open-codebase-index` is a promising backend for that contract.

The pilot must also verify the claims rather than trusting the backend's
metadata. In particular, it must detect branch switches, dirty files, stale
indexes, and linked worktrees correctly.

## Pilot Roles

| Candidate | Primary role | Overlap with current stack | Decision |
|---|---|---|---|
| `open-codebase-index` | Branch/worktree-aware code index and provenance | Medium: retrieval, symbols, call graphs | P0 pilot |
| Codanna | Semantic search with pre-correlated symbol and impact context | Medium-high: retrieval, call graph, impact | P0 pilot |
| SCIP | Commit-pinned definitions, references, and implementations | Low at the protocol level; overlaps at query level | Long-term track |
| Serena | Live, type-resolved navigation and safe edits | Callers/references overlap conceptually | Keep as correctness authority |
| Current graphify layer | Fast structural graph and clustering | Direct overlap with graph backends | Keep as baseline and fallback |
| Probe | Zero-index live AST context and token-bounded extraction | Medium: structural search and context | Later pilot if needed |
| Semgrep | Structural bug and security findings | Low for navigation | Separate analysis track |
| CodeQL | Dataflow, taint, and security analysis | Low for navigation | Separate analysis track |
| Gortex | Broad persistent graph and multi-repo intelligence | High | Benchmark only for now |
| Semble | Local embedding plus BM25 retrieval | High with CocoIndex retrieval | Do not prioritize |
| ast-grep | Structural search, lint, and rewriting | High with `CodePattern` | Do not prioritize |
| Kythe | Large semantic graph platform | Potentially high | Too operationally heavy for now |
| Sourcebot | Self-hosted search and navigation product | High | Licensing and product-scope concerns |

## SCIP Direction

SCIP is strategically different from the MCP servers above. It is a
language-neutral protocol and protobuf index format for definitions,
references, and implementations. It gives Engram a path to consume semantic
indexes from multiple language-specific producers without coupling the gateway
to one vendor's graph schema or tool catalog.

The intended division is:

- Serena supplies live, type-resolved answers for the current checkout and
  supports edits.
- SCIP supplies reproducible, commit-pinned semantic snapshots that can be
  indexed, cached, compared, and attributed to a branch or revision.
- CocoIndex supplies conceptual retrieval and current document/code search.
- The existing graphify layer supplies fast structural graph queries and
  clustering.

SCIP does not solve live uncommitted state by itself. It requires an index
producer and a consumer/facade, so it should initially be evaluated as a
snapshot and provenance capability rather than added as a fourth raw MCP
server. TypeScript, Python, and Rust are reasonable first languages to test;
Go can continue using Serena/gopls until an equivalent SCIP path is proven
useful.

## Gateway Integration Guardrails

All pilots should remain optional backends behind the existing Engram gateway.

- Do not run vendor installers that rewrite OpenCode, Codex, or other agent
  configuration.
- Launch candidates directly through the gateway's existing subprocess or HTTP
  backend configuration.
- Pass an explicit absolute project/worktree path or project flag. The current
  stdio adapter has command, argument, and environment support but no general
  child `cwd` field.
- Do not model branch selection as process-global state. The project and branch
  must be selected per route or MCP session.
- Expose a narrow allowlist, not every tool in a large backend catalog.
- Configure local embeddings or provider-free modes during evaluation. No code
  should be sent to a remote embedding provider implicitly.
- Add an Engram-owned provenance envelope around results, even when the
  backend's native response omits it.

The minimum provenance fields should be:

```text
repository
worktree
branch
current_commit
indexed_commit
indexed_at
includes_uncommitted
stale_or_warning
```

## Evaluation Plan

Use the existing representative language set: Engram Python, Kubernaut Go,
Koku Python, Praxis Rust, and RHDH TypeScript/TSX.

Measure:

- Exact symbol, reference, and implementation precision against Serena/gopls,
  pyright, rust-analyzer, and TypeScript language-server results
- Concept-search precision compared with CocoIndex hybrid search
- Call-graph and impact accuracy compared with the existing graphify layer
- Branch switching, linked worktrees, dirty files, and stale-index warnings
- Cold indexing time, incremental update time, warm query latency, CPU, and RSS
- Returned tokens and number of MCP calls needed for a representative task
- Restart, corruption, dependency, and degraded-backend behavior
- License, supply-chain, and local-data handling requirements

No pilot should become the default merely because it has more tools or a higher
GitHub star count. The acceptance bar is a demonstrated improvement for a
specific Engram query class.

## First `open-codebase-index` Pilot Result

The first pilot ran against the live Kubernaut checkout without changing that
worktree or any agent configuration:

```text
package: open-codebase-index@0.31.1
project: /Users/jgil/go/src/github.com/jordigilh/kubernaut
branch: fix/2442-workflow-discovery-membership
HEAD: 946d117d9db345344a17762e190d9c317ff83ee0
mode: structural (provider-free), scope: global
```

The index completed in 158.1 seconds and processed 5,169 files into 57,305
structural chunks. Three files were reported as not indexed because they were
coverage or unsupported-markup artifacts. Status then reported the active
branch catalog as ready, with `base branch: main`, `provider: none`, and all
57,305 chunks available for the active branch. The persisted SQLite metadata
also recorded the exact branch commit above, although the human-readable
`ocbi status` output did not show that commit.

The structural search path returned the expected Kubernaut workflow-discovery
results. Definition lookup resolved `buildFinalResult` exactly, and its callee
graph resolved six local calls while reporting
`InjectTargetResourceParameters` as unresolved. A Go method definition was
discoverable with the qualified name `Validator.SetDiscoveredWorkflowState`,
but graph lookup by that qualified method name did not resolve it. This is
useful navigation, but it is not a replacement for Serena's type-resolved
references and implementations.

The index was built from the working tree, not only committed bytes: a search
found the new, uncommitted comment in
`test/services/mock-llm/conversation/conditions.go`. That validates dirty-file
content inclusion, but the CLI status did not expose an explicit
`includes_uncommitted` field or stale warning. Branch catalog isolation was
observed for the active branch, but branch switching and linked-worktree
behavior were not yet exercised.

This result is a successful technical pilot, but it does not justify adding
`open-codebase-index` as another production backend now. Its incremental value
does not yet outweigh the extra index, daemon/configuration path, storage
lifecycle, and routing complexity. Keep the pilot artifacts and findings, but
do not integrate or expose it through the gateway unless a concrete
branch/worktree debugging workflow demonstrates that the current stack cannot
meet the need. A future reconsideration still requires an Engram-owned
provenance envelope containing current and indexed commits, dirty-state
detection, and stale warnings, plus a second-branch/worktree test.

## Codanna P0 Pilot Result So Far

Codanna `0.16.0` was installed into an isolated temporary root and evaluated
against the same Kubernaut working tree. Its index was kept outside the
repository and semantic search used the local `AllMiniLML6V2` embedding model;
no generative LLM, API key, Ollama server, or remote source upload was needed.

The structural index contained 112,361 symbols across 3,153 files and 27,463
relationships. The semantic index added 16,794 local embeddings at 384
dimensions and occupied about 81 MB, compared with about 46 MB for the
structural-only index. A warm one-shot semantic context query completed in
about 0.8 seconds including CLI startup.

Codanna resolved the Go method
`SetDiscoveredWorkflowState` directly, where the open-codebase-index graph
lookup did not. It also resolved all six `buildFinalResult` callees, including
`InjectTargetResourceParameters`, and returned its caller and two-symbol impact
chain. The semantic context query for merging the RCA with selected workflow
details found `buildFinalResult` and returned the symbol, documentation, six
callees, caller, type use, and impact guidance in one response. The native MCP
stdio handshake and the same context query also passed.

Against the same representative queries, CocoIndex returned a useful related
set of snippets but did not rank `buildFinalResult` first for the query
"merge RCA with selected workflow details from discovery result". Codanna
returned `buildFinalResult` first and included its relationships in the same
response. For "workflow discovery membership", the two systems surfaced
different but relevant parts of the flow: CocoIndex emphasized the selection
authorization and catalog-discovery code, while Codanna emphasized the
discovery-state symbols. This is evidence of complementary retrieval, not a
general quality victory; it is only a small representative sample.

This is a stronger fit for a possible complementary backend than
open-codebase-index because its distinctive value is the compact semantic
context pack rather than another status/index surface. The result is still
preliminary: branch/worktree provenance, stale handling, and dirty-file
behavior remain open. Keep Codanna as a P0 evaluation candidate, but do not
add it to the production gateway until those integration and quality checks
are measured.

### Matched CocoIndex Comparison

The initial comparison was invalid because the CocoIndex mirror was a broken,
stale Git worktree that predated the workflow-discovery changes. CocoIndex was
backfilled from the current Kubernaut checkout on 2026-09-22 before repeating
the probes. Codanna was also fully rebuilt from the same checkout; the current
index contains 112,703 symbols, 27,562 relationships, and 16,817 local
embeddings across 3,159 files. The refreshed comparison remains small, but it
changes the recommendation:

| Query class | CocoIndex hybrid | Codanna semantic context |
|---|---|---|
| Workflow membership before selection | Ranked `IsAllowed`, `authorizeSelectionDriver`, and `SetDiscoveredWorkflowState` in the top three | Ranked `SetDiscoveredWorkflowState` and `IsAllowed` first |
| Investigator self-correction and catalog validation | Ranked `selfCorrectWorkflowSelection` first and returned the retry/catalog path | Also ranked `selfCorrectWorkflowSelection` first, followed by human-review/retry and catalog-validation symbols |
| Catalog filtering by action type and signal context | Ranked `ListActions` and `ListWorkflowsByActionType` first | Ranked `ListWorkflowsByActionType` and `filtersFromSignal` first |
| Exact `SetDiscoveredWorkflowState` lookup through semantic search | Target appeared at rank 2 | Target appeared at rank 4 behind `WithDiscoveredWorkflowState` |

The refreshed sample makes Codanna a credible semantic-search replacement
candidate: it produced slightly better top-five coverage and mean target rank,
while CocoIndex produced more top-one hits. The systems return different
result granularity, though: CocoIndex returns larger code chunks while Codanna
returns compact symbol/context packs. Keep both enabled until task-level
evaluation confirms that Codanna's symbol-level results are sufficient for real
agent tasks. CodeGraph remains the authoritative graph/reference backend.

The reproducible benchmark is
`scripts/benchmark_semantic_search.py`. Against sixteen Kubernaut queries on
the same current worktree, using Codanna's structured `semantic_search_docs`
tool:

| Metric | CocoIndex hybrid | Codanna structured symbols |
|---|---:|---:|
| Top-1 target | 6/16 | 5/16 |
| Target in top 5 | 11/16 | 12/16 |
| Mean target rank | 2.18 | 2.08 |
| Approximate response tokens | ~1,219 | ~973 |

CocoIndex's output contains `filepath`, `chunk_index`, score fields, and raw
code text. Codanna's structured output contains symbol ID/name/kind, file path,
line range, signature, documentation, module, language, and score. Codanna's
`semantic_search_with_context` adds callers/callees and impact context, but the
additional context increased the response-size proxy substantially in this
probe; it should be used after the compact symbol search selects a target, not
as the default first pass.

The benchmark also exposed a freshness defect: before the source-boundary fix,
CocoIndex embedded the read-only mirror while Codanna indexed the live
checkout. Code ingestion and code pattern search now default to the live
Kubernaut-family checkouts; docs/issues continue using stable mirrors. The
release-line mirrors remain supplementary. This is required for any fair
semantic-search comparison because a stale index can look like a ranking
failure when it is actually missing the current branch's symbols.

The current deployment is still a single shared Kubernaut code-ingestion/search
process. It follows branch changes inside the configured live checkout, but
separate simultaneous worktrees need separate scoped processes or a future
worktree/commit namespace in `code_embeddings`; they must not share one
unqualified row set. That is an explicit follow-up before claiming full
multi-session branch isolation.

## Historical Kubernaut Codanna Evaluation

Codanna `0.16.0` was enabled on 2026-09-21 as a temporary semantic-search
experiment and removed from the active Kubernaut client configuration on
2026-09-23. The current working-tree gateway registry now configures Kubernaut's
code route with `ZvecShadowRelayAdapter`: zvec-grep is primary, and CocoIndex
search plus comparable graph queries run asynchronously as a shadow. Other
Kubernaut-family routes continue using the shared CocoIndex backend.

The primary URL defaults to `http://127.0.0.1:7999/mcp` and can be overridden
with `ZVEC_GREP_MCP_URL`; the shadow URL defaults to
`http://127.0.0.1:8891/mcp` and can be overridden with `COCOINDEX_MCP_URL`.
The comparison log is `~/.engram/logs/zvec-cocoindex-shadow.jsonl`. It records
query/tool arguments, canonical root, branch, commit, dirty state, both full
responses, per-backend latency, and shadow errors. A shadow is issued only when
the absolute root exactly matches a configured CocoIndex live source; this
avoids comparing unrelated or unindexed worktrees. The primary response is
returned without waiting for the shadow. Each completed entry also identifies
CocoIndex as the comparison reference and records unique-file rank overlap and
rank deltas for semantic queries, or caller-by-depth and resolver-count
differences for graph queries. These are continuous discrepancy indicators,
not adjudicated relevance labels. The replay snapshot is also retained in
`~/.engram/zvec-grep/live-shadow-acceptance-authoritative.jsonl`.

The zvec-grep agent toolset now exposes search and root-scoped callgraph
blast-radius, shortest-path, cluster, and communities tools; `full` adds index,
status, and managed-rg operations. The local CodeGraph MCP remains available
for exact, type-resolved navigation. This gateway change is implemented and
unit-tested and active behind the Kubernaut Engram route (container front door
8896 -> native gateway 8898 -> zvec HTTP daemon 7999). The first live replay
indexed the current worktree's matched production-Go scope at 1,071 files and
17,615 entities, then completed seven semantic comparisons and two graph
comparisons. Keep the gateway shadow log under observation; this exploratory
sample is not an acceptance decision.

On that seven-query snapshot, zvec and CocoIndex agreed on the top file for one
query; the other top-file rankings differed, with full CocoIndex-only and
zvec-only result lists retained in the JSONL comparison records. For
`SetDiscoveredWorkflowState`, both graph tools returned the same two-level
caller chain. Their graph-wide resolution counters differed substantially
(zvec: 81,033 calls, 42,705 unresolved, 21,791 ambiguous; CocoIndex: 67,292,
39,299, 15,997). Treat CocoIndex as the comparison reference, investigate the
scope/resolution deltas, and do not treat either backend's unadjudicated ranks
as relevance labels. zvec's `possibly_stale` header here accompanies
`served_from_current_index`; index status was ready and returned hits had no
per-item stale flags.

The local zvec daemon and Engram gateway are now running. Subsequent Kubernaut
calls through the 8896 front door are automatically shadowed and compared in
`~/.engram/logs/zvec-cocoindex-shadow.jsonl`; zvec configuration is under
`~/.engram/zvec-grep`, and branch-bound index/graph files are under the
Kubernaut root's locally excluded `.zvec-grep/` directory.

### Historical Codanna shadow relay

During the evaluation, the relay was `scripts/codanna_shadow.py`. Codanna's semantic MCP responses were
converted from its current formatted text into `codanna-shadow.v1` structured
records containing rank, symbol, kind, score, file range, signature, and
documentation. The original Codanna response is retained in the local shadow
log at `~/.engram/logs/codanna-cocoindex-shadow.jsonl`. For
`semantic_search_docs` and `semantic_search_with_context`, the relay starts a
bounded background CocoIndex search and records both engines, arguments,
branch, commit, latency, responses, and any shadow error. The CocoIndex call
does not delay or alter the primary result, and non-semantic Codanna tools are
forwarded without a shadow call.

Useful comparison fields can be inspected with:

```sh
jq -c '{query: .arguments.query, branch, commit, codanna_ms: .primary.elapsed_ms, cocoindex_ms: .shadow.elapsed_ms, codanna: .primary.response.structuredContent.results, cocoindex: .shadow.response.results, error: .shadow.error}' ~/.engram/logs/codanna-cocoindex-shadow.jsonl
```

Evaluate it using successful task completion, exact-symbol correctness,
semantic-search usefulness against CocoIndex, impact/caller correctness,
follow-up MCP call count, latency, stale-index incidents, and whether the
combined tool surface causes agent confusion. Keep CodeGraph for authoritative
graph/reference queries; do not use Codanna as its replacement.

The first live probe exposed two correctable issues. A semantic question about
where workflow membership is enforced returned the relevant
`Validator.IsAllowed` and `SetDiscoveredWorkflowState` methods, but a broad
three-step discovery query included unrelated context. Narrowing the query to
"selected workflow not returned by list_workflows validation membership"
returned the relevant `Contains`, `WorkflowDiscoveryContains`, and `IsAllowed`
symbols.

The initial impact query for `SetDiscoveredWorkflowState` returned no callers
even though the source contains a production call at
`internal/kubernautagent/investigator/investigator_workflow_selection.go:265`.
Codanna's Go resolver does not infer receiver types from factory/interface
return initializers. An explicit `*parser.Validator` local temporarily made
Codanna find the caller and four impacted symbols, but that application-code
workaround was reverted: Kubernaut should not be refactored around an indexer
limitation. The original source still passes its investigator package tests,
and Codanna returns zero callers after a watch refresh.

This is not a project configuration or stale-index setting. The active
`.codanna/settings.toml` controls indexing paths, embeddings, watcher timing,
and guidance; it has no receiver-inference switch. Codanna `0.16.0` documents
factory-call initializers as intentionally unbound, so the durable fix must be
in Codanna or a different type-aware fallback, not in Kubernaut's source.

## Branch Overlay Manager Spike

The branch overlay spike now has a backend-neutral Git planner in
`src/engram/code_overlay.py` and a compiled Go planner in
`spikes/code-overlay-go`. Both planners collect the merge-base delta, staged
and unstaged changes, untracked files, renames, deletions, content hashes, and
provenance. The same manifest can materialize a selected-file Codanna index or
an isolated CocoIndex table/state database. Codanna and CocoIndex use separate
eligible-file filters because Codanna rejects YAML/config files that the
Kubernaut CocoIndex flow accepts.

The Go and Python planners produced the same changed-path set and statuses for
the live Kubernaut worktree. Across repeated cold process runs, both planners
took roughly one second for this unusually dirty worktree; Git subprocesses and
file hashing dominate, not the implementation language. A compiled Go planner
does not yet justify a Rust rewrite. The production manager should therefore
be a long-lived Go process with cached manifests and filesystem-triggered
refreshes, avoiding a full Git scan on every MCP request.

The backend timing is materially different: a one-file Codanna overlay built
in about 1.6 seconds, while an overlay containing roughly 140 Codanna-eligible
files did not complete within ten minutes. This confirms that overlay
materialization and embedding/index construction, not the manager language,
are the current performance bottleneck. Rust should only be reconsidered if
profiling a cached/long-lived Go manager shows manifest or staging overhead is
materially affecting response latency.

A one-file CocoIndex overlay also completed successfully in about 14 seconds
including flow/model startup, and the resulting isolated table returned a live
semantic search result. The search layer accepts a validated table name for
this isolated path; the production MCP tool still defaults to the main table
until provenance and merge behavior are approved.

### Codanna overlay options measured

The initial ten-minute result was caused primarily by the three changed
generated OpenAPI Go files, not by the Go planner. The files were approximately
1.4 MB, 1.4 MB, and 242 KB. The following measurements were taken on the
heavily dirty Kubernaut worktree and are directional because the worktree was
changing during the spike:

| Strategy | Measurement | Trade-off |
| --- | --- | --- |
| One-file structural overlay | about 2 seconds | No semantic embeddings |
| Three generated files, semantic | about 230 seconds | Complete semantic overlay, but unacceptable latency |
| Three generated files, structural only | about 103 seconds | Still expensive due parsing/index writes |
| Non-test, non-generated files, semantic | about 93 seconds for 33 files | Better scope, but still background work |
| Four parallel semantic shards | about 22 seconds wall time | Semantic retrieval works; cross-file Codanna relationships are lost |
| One-file update on a copied base index | about 51 seconds | Existing index save rewrites large global state |

The spike now supports `--codanna-production-only` to exclude test/e2e/
integration and generated files, `--codanna-no-semantic` for a structural-only
build, and generated configs no longer retain `indexed_paths`. The latter is
important: retaining those paths caused every later one-shot query to re-index
the overlay. A config with no persistent paths can query the completed index
without that re-index step.

The options are therefore:

1. Use a long-lived Codanna watcher and refresh asynchronously. This preserves
   full graph semantics, but its index write cost must happen in the background.
2. Build a production-only overlay. This reduces scope and excludes files that
   rarely improve semantic answers, but is still not reliably interactive for a
   large branch delta.
3. Build structural-only first, then add semantic embeddings asynchronously.
   This gives exact symbols and relationships sooner but does not make large
   parsing/index writes free.
4. Build several semantic shards in parallel and merge ranked search results.
   This is the fastest Codanna semantic spike, but graph tools must continue to
   use the base index or Serena/CodeGraph rather than the shards.
5. Use CocoIndex as the semantic delta backend. Its isolated table supports
   incremental/content-addressed ingestion more naturally; the one-file live
   build and search path already work, but multi-file scaling remains to be
   measured.

The recommended next architecture is option 1 for Codanna structural
freshness, option 5 for semantic deltas, and option 4 only as a bounded
fallback when semantic results are needed before the full overlay is ready.
No query should synchronously wait for a full branch overlay; return the last
verified overlay with an explicit freshness state while the replacement builds.

### zvec-grep ephemeral semantic overlay

`zvec-grep` is a stronger fit for the ephemeral semantic half than the Codanna
alternatives. The isolated spike used `@zvec/zvec-grep` 0.2.2 in direct mode,
with a temporary staged root containing 220 changed files:

| Operation | Measurement |
| --- | ---: |
| Initial hybrid index | 18 seconds |
| Index size | 48 MB |
| Indexed entities | 10,588 |
| Hybrid query, including process startup | about 310 ms |
| One-file incremental update | about 5 seconds wall time |
| Explicit index drop | about 0.3 seconds |

The index stores under `<overlay-root>/.zvec-grep`, supports BM25 plus vector
retrieval, returns source paths and line ranges, and can be removed with
`zg index --drop --yes` or by deleting the temporary root. Direct mode ties
model/index lifetime to the foreground process, so this does not require a
watcher or resident branch daemon. The benchmark used
`local/potion-code-16m-v2`; the default code file-size limit was raised to 2 MB
to include the generated OpenAPI files.

The local zvec-grep `spike/codegraph-sidecar` work adds a Rust `zg-codegraph`
crate and `zg graph` / `zg graph-query` CLI commands for Go call graphs,
blast radius, shortest path, and Leiden clustering. That graph API is not yet
registered as a zvec-grep MCP tool, so the current MCP deployment continues to
use CocoIndex for Graphify-style graph queries. zvec-grep remains appropriate
for semantic retrieval over the changed-file delta. The merge design is:

1. Plan Git changes and tombstones with the existing backend-neutral planner.
2. Stage only current changed files into a content-addressed temporary root.
3. Build/query a direct zvec-grep index for that root.
4. Suppress base semantic hits for changed and deleted paths, then combine the
   overlay and base ranked lists using rank fusion rather than incomparable raw
   vector scores.
5. Remove the temporary root after the overlay is superseded or expires.

This avoids Codanna's global index rewrite, watcher lifecycle, shard graph loss,
and branch-resource leakage. The next implementation spike should wrap the
direct zvec-grep lifecycle behind the existing overlay manager and compare
overlay/base rank quality against the current CocoIndex shadow results.

The current preflight intentionally reports base-index provenance as unverified
until a persistent base-index manifest proves that the base index represents
the exact merge-base commit. Overlay materialization can be run for the spike,
but base-plus-overlay result merging must remain disabled until that check
passes.

## References

- [CocoIndex hybrid and structural search](COCOINDEX.md)
- [Graphify-inspired call-graph design](CALL_GRAPH_DESIGN.md)
- [Call-graph findings and measurements](CALL_GRAPH_CLUSTERING.md)
- [Hindsight, CocoIndex, and Serena division of labor](README.md#hindsight-vs-cocoindex-vs-serena-division-of-labor)
- [Issue #107: Improve recall and code search for current worktree debugging](https://github.com/jordigilh/engram/issues/107)
- [Serena](https://github.com/oraios/serena)
- [open-codebase-index](https://github.com/Helweg/open-codebase-index)
- [Codanna](https://github.com/bartolli/codanna)
- [SCIP](https://github.com/scip-code/scip)
