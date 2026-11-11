# rgctl-Informed Semantic Search Enhancements for zvec-grep

**Status:** Enhancement proposal

**Date:** 2026-11-11

**Tracking:** [GitHub issue #189](https://github.com/jordigilh/engram/issues/189)
is the implementation tracker for this proposal. The design was created on
branch `docs/rgctl-semantic-enhancements`.

**Scope:** Improve semantic code-search relevance and live-index efficiency in
zvec-grep by incorporating useful ideas from rgctl. This is not a proposal to
replace zvec-grep with rgctl.

## Executive summary

zvec-grep already has the correct primary contract for Engram's interactive
worktree profile:

- hybrid vector plus FTS retrieval over source fragments;
- local or explicitly authorized embedding providers;
- staged, unstaged, untracked, deleted, and renamed worktree handling;
- freshness barriers and provenance-bearing workspace indexes;
- Streamable HTTP MCP integration; and
- a versioned `codegraph-v2` sidecar with graph queries for the supported
  languages.

rgctl is valuable as a source of retrieval and ranking techniques rather than
as a replacement backend. Its semantic subsystem combines function-level
representations, identifier/body token sketches, late fusion, graph-aware
signals, optional graph diffusion, neighborhood expansion, and incremental
content-hash reuse.

The recommended direction is to preserve zvec-grep's current hybrid candidate
generation and add bounded, measurable second-stage improvements:

1. explicit symbol/body representation profiles;
2. compact identifier and body-token sketches;
3. graph-aware late fusion with capped, high-confidence signals;
4. bounded graph neighborhood expansion; and
5. per-fragment embedding reuse for faster live refreshes.

Community-aware retrieval is a follow-up. Quantized Hamming search, a compiled
vocabulary embedder, full CFG/PDG, clone detection, and security analysis are
separate performance or analysis tracks and are not first-wave semantic-search
changes.

## Design constraints

The enhancements must preserve these existing contracts:

- **Hybrid retrieval remains primary.** Graph signals may improve ordering or
  add bounded context, but must not replace strong lexical or vector evidence.
- **Worktree freshness remains authoritative.** Every semantic and graph signal
  must identify the same source snapshot, branch, and dirty-file state.
- **Serena remains the correctness authority** for type-resolved definitions,
  references, implementations, diagnostics, and edits.
- **No silent backend fusion.** A zvec result must be explainable from one
  zvec index generation and its associated graph artifact.
- **Ambiguity is preserved.** Unresolved or ambiguous graph edges cannot be
  treated as definite evidence in ranking.
- **Results remain source-grounded.** Search may rank an entity projection,
  but returned evidence must continue to point to bounded source fragments and
  ranges.
- **No remote data movement by default.** Local embedding and local graph modes
  remain the default evaluation path.

## Shared generation and publication contract

Semantic and graph artifacts must be published as one generation. A new
Engram/zvec-owned generation sidecar should extend the existing workspace
manifest rather than create an independent freshness authority. The proposed
sidecar is `.zvec-grep/generation.json` (or an equivalent versioned extension
of `manifest.json`). The workspace indexing coordinator owns this publication
path; the existing `codegraph-publication-v1.json` remains a subordinate graph
publication whose digest and manifest key are recorded in the shared
generation.

The canonical `source_digest` is SHA-256 over the bytewise-sorted sequence of
length-delimited records for every source file admitted by the index selection:

```text
uint64_be(utf8_byte_length(relative_posix_path)) || relative_posix_path ||
uint64_be(byte_length(file_bytes)) || file_bytes
```

Paths are repository-relative POSIX paths; the digest uses the actual staged,
unstaged, and untracked worktree bytes, omits deleted files and `.zvec-grep`
outputs, and records the selection/ignore configuration in
`source_selection_digest`. `generation_id` is the SHA-256 of the RFC 8785
canonical JSON generation preimage containing the source digest, selection
digest, index/graph artifact digests, workspace identity, model/schema
versions, and feature configuration. `state` follows `building → ready` or
`building → aborted`; a ready generation is immutable.

Writers take an exclusive per-workspace lock before building. The lock records
an owner token, PID/host, start time, and lease; only the owner can publish or
abort it, and stale takeover requires a failed owner-liveness check. A reader
never consumes a `building` generation. The record contains at least:

```json
{
  "schema_version": 1,
  "generation_id": "…",
  "repository": "…",
  "worktree": "…",
  "branch": "…",
  "current_commit": "…",
  "indexed_commit": "…",
  "includes_uncommitted": true,
  "source_digest": "sha256:…",
  "source_selection_digest": "sha256:…",
  "feature_config": {"semantic_features_version": 1},
  "index": {
    "path": "…",
    "sha256": "sha256:…",
    "manifest_version": 1,
    "index_version": 2,
    "embedding_model": "…",
    "embedding_schema": "…"
  },
  "graph": {
    "path": "…",
    "sha256": "sha256:…",
    "publication_sha256": "sha256:…",
    "schema": "zvec-grep.codegraph",
    "version": 2,
    "relation_generation": 4
  },
  "state": "ready",
  "published_at": "…"
}
```

The coordinator creates and validates temporary index/graph artifacts first,
including the existing graph publication manifest, computes their digests,
derives `generation_id`, fsyncs the artifacts, and atomically replaces the
generation manifest last. Readers recompute the source and artifact digests and
verify that the workspace identity, source selection, index manifest key,
graph publication manifest key, model/schema versions, and feature config match
the generation. A mismatch, missing lock owner, non-ready state, or changed
authorized workspace causes rejection and baseline fallback. Legacy manifests
without this binding cannot enable graph ranking. This contract is required
before graph signals can affect production ranking.

## Current zvec-grep baseline

The current search pipeline already provides several capabilities that overlap
with rgctl:

| Existing capability | Current behavior | Consequence for this proposal |
| --- | --- | --- |
| FTS/vector retrieval | Separate FTS and vector routes with adaptive recall | Do not copy rgctl's basic two-stage candidate retrieval; improve the reranker instead |
| Hybrid fusion | Rank-reciprocal fusion with route corroboration | Add new signals as a bounded second stage, not as a replacement score |
| Structural metadata | Symbol name, scope, qualified name, signature, modifiers, and documentation are available to lexical/vector content | Add independent fields and weights rather than blindly appending more text |
| Multi-query search | Primary hybrid groups plus supplemental FTS/vector groups | Use this for controlled query-class experiments and graph expansion |
| Entity and fragment grouping | Fragments can resolve to a containing entity and retain evidence ranges | Add an explicit entity projection while preserving fragment evidence |
| Code graph | `codegraph-v2` supports definitions, imports, calls, inheritance, implementation, references, tests, and related queries for supported languages | Reuse the graph artifact for retrieval signals; do not create a second graph format |
| Worktree lifecycle | Incremental file replacement, manifests, freshness state, authorization, and root-scoped MCP calls | Any new semantic/graph artifact must participate in the same lifecycle |

The existing four-language qevals also show that some misses are ranking
problems rather than ingestion failures: relevant symbols can appear in FTS and
vector candidate lists while remaining too low in the final result. That is the
primary opportunity addressed here.

## Proposed enhancements

### E0. Retrieval diagnostics and feature gates

**Priority:** P0, prerequisite

Before changing ranking, expose enough per-hit information to tell whether an
enhancement helped:

- candidate rank from FTS and vector routes;
- normalized name/identifier overlap;
- token-sketch overlap;
- graph evidence and edge confidence;
- entity/body/document representation used;
- each reranker contribution and final score;
- index, graph, model, and source-snapshot fingerprints; and
- whether graph expansion added or only explained a result.

The existing search trace is the natural transport location. Keep detailed
traces opt-in or bounded so normal MCP responses do not grow unnecessarily.

Every experiment must have a feature flag and a stable run-manifest value. The
baseline must remain reproducible without the feature.

The proposed optional top-level request field is `semantic_features`. Its
absence preserves the current request and response behavior. Version 1 has
these defaults and validation rules:

```json
{
  "semantic_features_version": 1,
  "representation": "all",
  "token_sketch": "off",
  "graph_rerank": "off",
  "graph_expansion": "off",
  "trace": "off"
}
```

Defaults are `representation=all`, `token_sketch=off`, `graph_rerank=off`,
`graph_expansion=off`, and `trace=off`. An existing `trace: true` request is
translated to `trace=summary` by the compatibility adapter. Unknown feature
versions or enum values fail with a structured invalid-parameter error; they do
not silently downgrade. The allowed values are `representation ∈ {symbol,
body, document, all}`, `token_sketch ∈ {off, trace, rerank}`,
`graph_rerank ∈ {off, shadow, on}`, `graph_expansion ∈ {off, related}`, and
`trace ∈ {off, summary, full}`. `summary` contains at most the returned
primary hits and `full` contains at most 100 candidate rows, with no source
body text.

The versioned trace shape is:

```json
{
  "schema_version": 1,
  "generation_id": "…",
  "candidates": [{
    "entity_id": "…",
    "fts_rank": 1,
    "vector_rank": 4,
    "features": {"name_overlap": 0.8, "token_sketch": 0.2, "graph": 0.0},
    "contributions": {"base_hybrid": 0.8, "name_overlap": 0.08},
    "final_score": 0.88
  }],
  "graph": {"seed_ids": [], "eligible_edges": 0, "excluded_edges": 0},
  "expansion": {"enabled": false, "related_count": 0}
}
```

Trace fields are bounded, deterministic, and diagnostic; they do not change
the source-grounded result contract.

### E1. Explicit symbol, body, and document representations

**Priority:** P0

#### rgctl idea

rgctl can index functions or documentation separately. Its function index can
use declaration metadata alone or opt into function bodies, and its results
are stable function/document nodes rather than arbitrary text chunks.

#### zvec-grep adaptation

Keep the current fragment index and add a distinct searchable projection for
structural entities:

- symbol name and qualified name;
- signature and parameter/type information;
- documentation and framework annotations;
- body text; and
- parent module/file context.

The projection can be represented as an entity-level fragment or a second
logical route in the existing workspace index. It does not require a second
database. The search response should return the entity as the ranked target
and retain the best source fragments as evidence.

Support explicit representation modes:

- `symbol`: names, qualified names, signatures, and modifiers;
- `body`: implementation text and local documentation;
- `document`: Markdown headings, section scope, and links; and
- `all`: the current mixed behavior.

The default should remain compatible with the current hybrid behavior until the
qevals show a safe default.

#### Expected benefit

- fewer duplicate or fragmented hits for one function;
- improved exact-symbol and implementation discovery;
- better mapping from a conceptual query to a useful source unit; and
- cleaner normalization for relevance evaluation.

#### Risks

- entity projections can hide a necessary child fragment;
- long function bodies can dominate short but important signatures; and
- mixed code/document corpora can cross-contaminate ranking.

Use separate evidence and bounded representation weights rather than replacing
the current fragment content.

### E2. Identifier and body-token sketches

**Priority:** P0

#### rgctl idea

rgctl computes a compact 256-bit token-bloom sketch from split identifier and
body tokens. It uses the sketch for fast keyword filtering, overlap scoring,
and approximate structural similarity without rereading source files.

#### zvec-grep adaptation

At extraction time, compute a versioned sketch from:

- symbol and qualified-name parts;
- signature and type names;
- documentation terms; and
- body identifier tokens.

Store it as structured entity metadata, not synthetic embedding text. At query
time, compute the query sketch and expose two optional behaviors:

- **keyword anchor:** require or strongly prefer a high-confidence identifier
  overlap when the query contains an explicit symbol or distinctive term;
- **sketch rerank:** use sketch similarity as a bounded tie-breaker or second
  stage signal.

This extends the existing `identifierParts` and `preferSymbol` behavior without
making every conceptual query lexical-only.

#### Expected benefit

- exact and near-exact symbol queries become less vulnerable to semantic
  distractors;
- camelCase, snake_case, and qualified names match consistently; and
- the reranker gets a cheap signal that is less noisy than raw global graph
  popularity.

#### Risks

- hard keyword filtering can hurt paraphrase queries;
- common tokens can create false overlap; and
- sketch format changes require index-version invalidation.

Apply hard filtering only when the query classifier detects an explicit anchor;
otherwise use a capped score contribution.

### E3. Graph-aware late fusion

**Priority:** P0 after E0; first graph-ranking experiment

#### rgctl idea

rgctl reranks a semantic candidate pool using multiple signals, including
semantic similarity, exact token overlap, token-bloom overlap, PageRank, and
blast-radius features. It also supports graph diffusion over call neighbors.

#### zvec-grep adaptation

Add a second-stage reranker after the existing FTS/vector candidate collection:

```text
final_score = current_hybrid_score
            + capped_name_overlap
            + capped_token_sketch_overlap
            + capped_graph_signal
            + optional_representation_signal
```

The graph signal should initially be personalized and query-seeded:

1. select a small set of high-confidence hybrid seeds;
2. walk only selected, resolved edge types from the current `codegraph-v2`;
3. use direction appropriate to the query class, such as callers versus
   callees; and
4. apply a small capped boost or rank-fusion contribution.

The first ablation must not add values with incompatible scales to the current
RRF score. Normalize every signal to `[0, 1]` over the candidate pool and freeze
the initial experimental weights in the run manifest:

```text
rerank_score = 0.80 * base_hybrid
             + 0.10 * name_overlap
             + 0.05 * token_sketch
             + 0.05 * graph_signal
```

The graph contribution is therefore capped at five percent of the normalized
score. Existing deterministic ID ordering remains the final tie-breaker. These
weights are an evaluation arm, not a permanent default; later tuning must be a
new recorded arm.

Do not make raw global PageRank or total reference count a strong prior. Popular
utilities and framework hubs are frequent false positives. Keep unresolved and
ambiguous edges out of the first ranking arm.

Graph artifact `resolved` is a name/structure resolution flag, not a claim of
compiler-level type resolution. Therefore E3 defines two measured graph arms:

- **syntax-confidence arm:** an edge is eligible when `resolved == true`,
  `target` is present, `ambiguous_candidates` is empty, and its producer marks
  it `resolution=syntax` (or supplies no stronger resolution label). This arm
  is explicitly labelled syntax/name-based, keeps the five-percent graph cap,
  and is exploratory only;
- **strong-confidence arm:** the same target and ambiguity checks apply, but
  the edge must carry a versioned stronger producer label or an accepted
  language-specific call-facts context. This arm is eligible for production
  promotion after the qeval gate.

Both arms record the resolution label and producer context in the trace. An
empty strong-confidence edge set is a valid result that must be reported; it
must not be silently filled with syntax edges or presented as type-resolved.

#### Expected benefit

- architecture and lifecycle questions can surface connected implementation
  sites;
- caller/callee-oriented questions can prefer structurally relevant symbols;
- graph information improves ordering without destroying direct lexical hits;
  and
- the current zvec candidate generator remains the recall authority.

#### Risks

- stale graph artifacts can rank deleted or moved code;
- centrality can overpower direct relevance;
- graph resolution errors can amplify false edges; and
- query-independent graph scores can make results less explainable.

The graph artifact and semantic index must share source, branch, model, and
freshness fingerprints. Graph ranking remains an offline/shadow arm until the
qeval gate passes.

This is a follow-on to earlier zvec relationship-reranking and graph-assisted
recall experiments. Those experiments include neutral and regressing arms in
`benchmarks/semantic_search/PROPOSAL_QEVAL_REPORT.md`; their failure modes must
be carried forward, and their weights must not be reused as validated defaults.

### E4. Bounded graph-neighborhood expansion

**Priority:** P1

#### rgctl idea

rgctl can expand a semantic result into a bounded call-graph neighborhood after
the initial search. This supplies related functions without requiring the user
to know their names.

#### zvec-grep adaptation

After E3 seeds are selected, optionally retrieve one-hop or two-hop neighbors
from high-confidence `calls`, `imports`, `implements`, `references`, or `tests`
edges. Add expanded candidates with explicit provenance:

```text
expanded_from = <seed entity id>
edge = calls | imports | ...
hops = 1 | 2
confidence = definite | possible
```

Expansion must be bounded by candidate count, hop count, and score decay. It
must not silently replace the primary ranked list. The initial E4 contract is
to append an optional top-level `related` sibling to the existing primary
`items`/`groupResults` response. With `graph_expansion=related`, it contains at
most 20 items reached from at most the top five primary seeds, at no more than
two hops. Each item has the normal source identity (`entity_id`, `path`, and
`range`) plus this versioned provenance:

```json
{
  "entity_id": "…",
  "path": "src/example.ts",
  "range": {"start_line": 10, "end_line": 20},
  "expanded_from": "seed-entity-id",
  "edge": "calls",
  "hops": 1,
  "confidence": "syntax",
  "score_decay": 0.5,
  "generation_id": "…"
}
```

`confidence` is `definite` only for strong-confidence eligible edges and
`syntax` for the syntax-confidence arm. Related items are deduplicated by
`entity_id`; when multiple paths reach one entity, retain the highest
confidence, then fewest hops, then the lexicographically smallest seed/edge
path. Sort the final section by confidence, score decay, hops, and entity ID.
Related items do not count toward the primary top-k ranking or normalized
source-unit qeval cutoff, and are omitted entirely when expansion is off.
Promoting expanded items into the primary list requires a separate feature arm
and API decision.

This is safer for zvec-grep than index-time embedding diffusion because live
worktree queries can use the current graph artifact without permanently
smearing embeddings across stale relationships.

### E5. Incremental content-hash embedding reuse

**Priority:** P1

#### rgctl idea

rgctl stores a content hash and model fingerprint for semantic rows. Unchanged
functions reuse their existing quantized embedding during incremental updates.

#### zvec-grep adaptation

Extend the existing file/entity metadata with a content identity for each
searchable projection:

```text
projection_id
content_hash
embedding_model
embedding_schema
source_snapshot
```

When a file is reparsed, reuse embeddings for projections whose normalized
content and model fingerprint are unchanged. Re-embed only changed projections.
Handle renamed or moved files by separating content identity from path identity;
path changes still update provenance and file metadata.

Measure this independently from relevance. The goal is lower incremental index
latency and fewer freshness waits, not a different ranking.

Before acceptance, add and freeze
`benchmarks/semantic_search/fixtures/incremental-embedding-reuse-v1/workload.json`.
It must name a base fixture, exact repository-relative paths, byte ranges and
replacement bytes for the one-symbol edit, the rename source/destination, the
deleted file, and the expected unchanged projection IDs. Each scenario resets
to the same base source digest before running.

For each same-engine baseline/candidate pair, perform two unmeasured warm-up
refreshes, then ten measured repetitions. The baseline disables reuse and the
candidate enables it; both use the same model, cache, staged workspace, and
generation publication path. Timing starts immediately before the refresh call
and ends after the ready generation is durably published; process startup and
model download are excluded. An embedding call is one projection supplied to
the model's embed operation. Report embedding-call count, reuse percentage,
freshness failures, and p50/p95 using the nearest-rank percentile over the ten
measured repetitions.

Required scenarios are:

- no-op refresh: zero changed projections are re-embedded;
- one-symbol edit in a multi-symbol file: at least 90% of unchanged projections
  are reused and candidate p95 is at most `max(1.10 * baseline_p95,
  baseline_p95 + 50ms)`;
- file rename/move: content embeddings are reused while path and provenance
  fields are updated; and
- deletion: removed projections are absent from both retrieval and graph
  artifacts after publication.

### E6. Community-aware retrieval and diversity

**Priority:** P1/P2

E6 is explicitly outside the first-wave acceptance scope of issue #189. It is a
follow-up experiment after E1–E5 establish stable entity and generation
identities.

#### rgctl idea

rgctl computes Leiden communities and can query community-level semantic
representations. Communities provide a way to search or summarize a large
repository without treating every function as an unrelated candidate.

#### zvec-grep adaptation

Use the existing codegraph community assignments as optional retrieval metadata:

- restrict a query to a selected community after an initial broad pass;
- diversify the top results across communities;
- boost a community only when multiple high-confidence seeds agree; or
- retrieve one representative entity per community before expanding locally.

Do not use community membership as an unconditional relevance prior. Community
assignments must be rebuilt or invalidated with the graph snapshot.

## Features to defer

These rgctl features are useful, but they should not be the first semantic
search additions:

| Feature | Reason to defer |
| --- | --- |
| 1-bit sign-quantized vectors and POPCNT search | Primarily a memory/latency optimization; requires relevance and recall validation against the current embedding model |
| Compiled vocabulary embedder | Useful provider-free fallback, but not evidence of better semantic relevance than `potion-code-16m-v2` |
| Full CFG/PDG/CPG | Valuable flow analysis, but a separate analysis capability with high extraction and freshness cost |
| Clone detection | Useful for maintenance and review, not a general semantic retrieval signal |
| OSV, dependency, and taint analysis | Separate security workloads; keep them out of the semantic ranker |

## Evaluation plan

### Paired feature arms

Use the existing four independent qeval lanes:

1. current zvec-grep baseline;
2. E1 symbol/body projection;
3. E2 token sketch and identifier anchoring;
4. E3 graph-aware late fusion;
5. E4 bounded neighborhood expansion;
6. E5 incremental reuse latency arm; and
7. a combined arm containing only individually accepted changes.

### Engine and control matrix

The source-language lanes and implementation engines are separate dimensions:

| Dimension | Required values |
| --- | --- |
| Source language | Go, Python, Rust, TypeScript |
| Implementation engine | TypeScript `zg`, Rust `zg` |
| Arm | Same-engine baseline and candidate |
| Control | Same fixture, qrels, model, device, flags, and result limit |
| Graph arm | Both baseline and candidate use the same graph generation; only the candidate ranking/expansion feature differs |

E1, E2, and E5 target both engines. E3 and E4 may begin as Rust graph arms,
but must be labelled exploratory until an equivalent TypeScript arm exists. No
cross-engine aggregate is valid. Every `run-manifest.json` must record
`engine`, `control_commit`, `candidate_commit`, `feature_config`,
`index_version`, `graph_schema`, and `generation_id`.

The current replay option that builds a graph only for a candidate is
insufficient for a causal graph-ranking comparison. The graph arm must instead
stage one source workspace, build one graph generation, and load that same
graph digest/generation into both same-engine controls. The control disables
only the candidate graph feature; the candidate enables it. A graph result is
not acceptance evidence if the control and candidate graph generations differ
or if the runner cannot prove that both consumed the same artifact.

Hold constant for every relevance arm:

- fixture revision and source digest;
- qrels and query text/order;
- source selection and result limit;
- embedding model and model cache;
- CPU/device settings;
- normalizer and scorer; and
- index and graph artifact versions.

The Kubernaut real-repository suite remains exploratory evidence. It must not
replace the source-authored four-language quality gate.

### Metrics

Report per language, implementation engine, and query category:

- normalized source-unit Recall@10;
- normalized source-unit nDCG@10;
- normalized source-unit MRR@10;
- normalized source-unit Precision@10;
- candidate-to-result rank movement;
- index and incremental-update latency;
- warm query latency, CPU, and RSS;
- returned tokens and follow-up reads; and
- graph expansion count and edge-confidence breakdown.

The current synthetic runner retrieves raw limit 10, then normalizes entity
fragments to source units before scoring the first ten normalized source units.
Thus the reproducible relevance gate for issue #189 is source-unit Recall@10,
nDCG@10, MRR@10, and Precision@10. Recall@50 requires a separate deep-
candidate runner and is intentionally out of scope.
Success@5 remains a future task-level agent-workflow metric, not an acceptance
criterion for this document.

Do not compare raw model scores across arms that use different representation
or embedding models. Preserve full ranked results and feature traces for
diagnosis.

### Acceptance gate

Apply the existing no-regression rule independently to every
source-language × implementation-engine lane (Go, Python, Rust, and
TypeScript × TypeScript `zg` and Rust `zg`):

- at least one tracked relevance metric must improve;
- the other tracked relevance metrics must not decline;
- every query-level regression must be reviewed; and
- a regression in any language/engine lane blocks a universal-improvement claim.

For graph-aware arms, additionally require:

- no stale graph evidence in a fresh-index response;
- no increase in unresolved-edge-driven results;
- no hub-dominance regression on conceptual queries; and
- explainable provenance for every graph-boosted or expanded result.

The current scorer and runner are the authoritative implementation of the
synthetic gate; do not report an unsupported cutoff or metric from the existing
raw-limit-10 artifacts.

## Rollout sequence

### Phase 1: Instrumentation and entity projection

- Extend the bounded search trace with representation and score components.
- Add E1 behind a feature flag.
- Run the four-language baseline and E1 paired arms.

### Phase 2: Identifier and graph ranking

- Add E2 token sketches and query-class-aware anchoring.
- Add E3 as an offline/shadow reranker over the existing candidate pool.
- Compare per-query rank movement and relevance deltas.

### Phase 3: Expansion and refresh efficiency

- Add E4 bounded neighborhood expansion for selected query classes.
- Add E5 content-hash embedding reuse.
- Measure freshness latency and response-token changes separately from relevance.

### Phase 4: Repository-scale retrieval

- Evaluate E6 community-aware scope and diversity on larger repositories.
- Test branch switches, linked worktrees, staged/unstaged/untracked changes,
  deletions, renames, and stale artifact recovery.
- Promote only feature combinations that pass both relevance and freshness
  gates.

## References

- rgctl upstream reference: `v0.4.27`, commit
  [`4d4d1b3f7345bb4d27b25f8d7168c6b541d96b7e`](https://github.com/rgctl/rgctl/tree/4d4d1b3f7345bb4d27b25f8d7168c6b541d96b7e).
- rgctl semantic design:
  [`docs/design/semantic-search-design.md`](https://github.com/rgctl/rgctl/blob/4d4d1b3f7345bb4d27b25f8d7168c6b541d96b7e/docs/design/semantic-search-design.md).
- zvec-grep hybrid pipeline at commit
  [`338546953d16f3aa1aa1d73137af20315ba1ca2f`](https://github.com/zvec-ai/zvec-grep/tree/338546953d16f3aa1aa1d73137af20315ba1ca2f):
  [`index.ts`](https://github.com/zvec-ai/zvec-grep/blob/338546953d16f3aa1aa1d73137af20315ba1ca2f/src/engine/pipeline/search/index.ts)
  and
  [`fusion.ts`](https://github.com/zvec-ai/zvec-grep/blob/338546953d16f3aa1aa1d73137af20315ba1ca2f/src/engine/pipeline/search/fusion.ts).
- zvec-grep embedding content projection at the same pinned commit:
  [`vector-content.ts`](https://github.com/zvec-ai/zvec-grep/blob/338546953d16f3aa1aa1d73137af20315ba1ca2f/src/engine/extraction/vector-content.ts).
- zvec-grep codegraph relation and query implementation at the same pinned
  commit:
  [`graph_queries.rs`](https://github.com/zvec-ai/zvec-grep/blob/338546953d16f3aa1aa1d73137af20315ba1ca2f/rust/crates/zg-codegraph/src/graph_queries.rs).
- Engram semantic-search evaluation protocol:
  `benchmarks/semantic_search/MULTILANGUAGE_PROTOCOL.md` and
  `benchmarks/semantic_search/QUALITY_EVALUATION.md`.
- Prior zvec retrieval ablations:
  `benchmarks/semantic_search/PROPOSAL_QEVAL_REPORT.md`.
- Engram semantic-code-intelligence decision:
  `docs/SEMANTIC_CODE_INTELLIGENCE.md`.
