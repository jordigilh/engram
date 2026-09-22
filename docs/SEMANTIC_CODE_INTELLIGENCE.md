# Semantic Code Intelligence: Findings and Pilot Decision

**Status:** evaluation decision; Kubernaut has a Codanna-primary shadow pilot

**Date:** 2026-09-22

**Related work:** [issue #107](https://github.com/jordigilh/engram/issues/107),
[Graphify-inspired call graphs](CALL_GRAPH_DESIGN.md), and the
[CocoIndex operations guide](COCOINDEX.md)

## Decision Summary

Engram will keep its current CocoIndex and Serena integrations as the baseline.
The next evaluation pilots are:

1. [`open-codebase-index`](https://github.com/Helweg/open-codebase-index) for
   branch-aware and worktree-aware indexing.
2. [`Codanna`](https://github.com/bartolli/codanna) for compact semantic context
   packs that combine retrieval, symbols, callers, callees, and impact.
3. [`SCIP`](https://github.com/scip-code/scip) as a long-term protocol and
   snapshot-indexing track, rather than as another broad MCP server.

This is an evaluation plan, not a decision to expose all three backends to
agents permanently. The Kubernaut pilot is deliberately scoped to one project:
Codanna is returned to the agent, while CocoIndex runs as an asynchronous
comparison only. A pilot must demonstrate a capability that the current stack
does not already provide, or measurably improve correctness, freshness,
provenance, latency, or token efficiency.

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

## Kubernaut Evaluation Configuration

On 2026-09-21, Codanna `0.16.0` was enabled for the Kubernaut project as a
temporary semantic-search experiment. On 2026-09-22, the local CodeGraph MCP
entry was restored alongside Codanna. The ignored Kubernaut `.mcp.json` now
starts CodeGraph, a Codanna-primary relay with the project-local ignored
`.codanna/settings.toml`, and Engram. Codanna still uses local
`AllMiniLML6V2` semantic search and file watching. The existing CodeGraph
binary and `.codegraph` index were not deleted. The current Kubernaut gateway
route no longer exposes the legacy CocoIndex code backend; other family routes
remain unchanged. This is a project-scoped evaluation, not an Engram
production-gateway adoption.

The relay is `scripts/codanna_shadow.py`. Codanna's semantic MCP responses are
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
