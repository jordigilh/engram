# Call-Graph Extraction + Clustering — Feasibility Study

Status: **spike landed on `engram` itself (2026-08-24)** -- extraction, clustering, and 3 MCP tools implemented and verified against this repo's own real, live checkout, cross-checked against Serena/gopls ground truth. See [issue #43](https://github.com/jordigilh/engram/issues/43) for the actionable summary and acceptance criteria; this document holds the detailed study and results so `docs/findings/2026-08.md` doesn't have to carry it in full. See "Spike results" below for what was actually measured; the sections after that are the original preflight, unchanged.

## Origin

`docs/findings/2026-08.md`'s 2026-08-07 entry ("Comparative Analysis Against Graphify-Labs/graphify") identified a real, unfilled capability gap in this stack: nothing here answers structural/relational questions about a codebase — call graphs, shortest path between two functions, blast radius, natural architectural clusters. That comparison deferred acting on it, citing Graphify's own pre-1.0 maturity risk and the resource cost of its dependency stack (`networkx`, `graspologic`/Leiden, a dozen `tree-sitter-*` grammars).

Separately, that same entry's "fourth/fifth follow-up" shipped `CodePattern`-based `*_code_pattern_search` MCP tools across all four onboarded projects (2026-08-07). This document reassesses the original gap now that CodePattern is live, asking: does CodePattern close any of it, and if not, what would it actually take to close the rest ourselves.

## Spike results (2026-08-24) -- engram only, live-rebuild-per-query

Implemented per the plan below: `src/engram/callgraph.py` (extraction + Leiden
clustering, `tests/test_callgraph.py`, 21 tests) plus three new MCP tools --
`engram_call_graph_blast_radius`, `engram_call_graph_shortest_path`,
`engram_call_graph_get_cluster` -- and matching CLI flags on the existing
`engram-cocoindex-search.py`. Scope, per the reviewed plan: prove the
pipeline on `engram`'s own small Python codebase, live-rebuild-per-query
only (no persistence), and cross-check the extraction heuristic's accuracy
against Serena/gopls ground truth before drawing conclusions.

### Measured numbers

| Metric | `~/.hindsight/watch/engram` (production `ENGRAM_REPO_DIR` default) |
|---|---|
| Build+extract time (fresh, no cache) | **0.92s** |
| Nodes (functions/methods found) | 1,423 |
| Edges (resolved call relationships) | 6,122 |
| Calls seen / unresolved | 8,784 / 6,599 (**75.1% unresolved**) |
| Clustering (Leiden, same graph) | sub-second, included in a `--cluster` call's total |

Live-rebuild-per-query is comfortably fast at this scale (well under 1s).
**Not yet measured**: engram is far smaller than the other three repos this
could eventually target (1,423 nodes here vs. 17,264/7,893/6,806+3,866
functions for kubernaut/koku/praxis-ai+praxis-grid per the original
preflight's scale table below) -- whether live-rebuild-per-query stays
viable at that scale, or whether persistence becomes necessary, is exactly
the phase-2 question this spike didn't answer and shouldn't be guessed at by
extrapolation. **Recommendation: measure once on one larger repo (`koku`,
the smallest of the big three) before deciding**, rather than persisting
prematurely off engram's numbers or deferring the decision indefinitely.

### The three known unknowns, resolved

1. **Nearest-preceding-definition heuristic accuracy** -- upgraded mid-spike
   from the originally planned "nearest preceding def by start line" to
   **containment via an indentation-approximated body span**, after
   discovering the simpler heuristic gets a very common idiom wrong: a call
   to something else that happens *after* a nested function's `def` line but
   is still inside the *outer* function's body (e.g. define a local closure,
   call it later in the same enclosing function). `CodePattern` only reports
   a def's *header* span (through the closing paren, or through the colon
   for the plain no-return-type case -- see the return-type bug below), never
   its body, so `callgraph.py` approximates a body's end by scanning forward
   from the header until a line at indentation <= the def's own indentation
   appears. This is a heuristic, not a parse (assumes reasonably consistent
   indentation), but it's strictly more accurate than line-proximity alone
   and correctly resolves all of: nested-function calls (inner vs. outer),
   a call inside a decorator's own argument list (correctly attributes to
   *no* function, not the textually-preceding one), a call at module level
   between two functions (same), and multi-line signatures. See
   `tests/test_callgraph.py::TestExtractCallSites` for the exact cases.
2. **Clustering meaningfulness** -- not boring. Leiden clustering on
   engram's real call graph produced a genuinely interpretable result: e.g.
   `find_code_files` and the pattern-search family land in a single ~90-100
   member cluster spanning all six near-identical `*_code_pattern_search`
   modules (dcm/engram/koku/kubernaut/praxis/rhdh_plugins) plus their tests --
   correctly reflecting that those six modules really are one duplicated
   subsystem, architecturally. This is real signal, not an artifact of a
   trivially small or degenerate graph.
3. **Unresolved-call ratio and correctness of what does resolve** -- the raw
   75.1% unresolved ratio mostly reflects that `engram` is thin
   Python glue calling into `psycopg2`, `mcp`, `cocoindex`, `networkx`,
   stdlib, etc. -- calls to code this repo doesn't define are *correctly*
   unresolved, not a sign of broken extraction. The Serena cross-check below
   measures the more important number: of the calls that *do* resolve, how
   many are actually correct.

**No wall was hit.** `match_code` distinguished `call` from
`function_definition` reliably throughout, including on real code far more
varied than the planning-time synthetic checks.

### An unplanned discovery: the def pattern silently dropped every type-annotated function

Verifying against engram's own real (heavily-typed) source surfaced a bug
the synthetic pre-spike check didn't: the originally planned def pattern,
`def \NAME(\(A*\)):` (with a trailing colon), does not match any function
with a return-type annotation -- `def f(x: int) -> int:` doesn't match
`...):` because `-> int` sits between the closing paren and the colon. This
silently dropped a large fraction of this repo's own functions (including
`pattern_search_code` itself) from the graph entirely. Fixed by dropping the
trailing colon (`def \NAME(\(A*\))`) -- still matches only
`function_definition`-kind nodes (a bare call is separately reported as kind
`"call"` by the same shape), and `CodeMatch.chunks[0].end.line` still lands
on the correct header line either way. Regression-tested in
`tests/test_callgraph.py::test_finds_function_with_return_type_annotation`.
Filed as a heuristic-tuning fix per the plan's own fallback guidance, not a
scope change -- no wall, no new dependency.

A second discovery, same verification pass: a same-file call to a common
helper name (e.g. each of the six `*_code_pattern_search` modules' own
private `_run_cli_pattern_query`) was resolving to *every* same-named
function repo-wide rather than the actual same-file one, corrupting
`shortest_path`/`blast_radius` results with an arbitrarily-chosen wrong
target. Fixed by preferring a same-file match when one exists, falling back
to the repo-wide (possibly multi-target) match only when nothing in the
calling file itself defines that name -- see
`tests/test_callgraph.py::test_same_file_call_prefers_same_file_target_over_other_files`.

### Serena/gopls cross-check: measured precision/recall, not just the unresolved ratio

Picked 5 real functions in this repo with non-trivial call relationships,
compared the heuristic's resolved caller edges against Serena's
`find_referencing_symbols` (type-resolved ground truth) for the same
functions:

| Function | Serena callers (ground truth) | Heuristic callers | Match |
|---|---|---|---|
| `chunking.py::find_code_files` | 12 | 12 | **Exact** (12/12) |
| `chunking.py::split_fixed_window` | 5 | 5 | **Exact** (5/5) |
| `callgraph.py::build_call_graph` | 6 | 6 | **Exact** (6/6) |
| `callgraph.py::compute_clusters` | 3 | 3 | **Exact** (3/3) |
| `search/engram.py::pattern_search_code` | 2 | 22 | 2 true positives + **20 false positives** |

Aggregate: **100% recall** (every real caller Serena found, the heuristic
also found, across all 5 functions -- zero misses) but **58.3% precision**
overall (28 true positives / 48 total reported), entirely dragged down by
one case. That one case is exactly the documented accuracy ceiling in
action: `pattern_search_code` is defined identically in six modules, and
test files that import *one specific* module's copy and call it
module-qualified (e.g. `dcm.pattern_search_code(...)`) get trailing-segment
resolved against *all six* modules' definitions, not just the one actually
imported -- because there's no same-file definition for the same-file
preference fix (above) to prefer, and resolving the actual import binding
would require real type/import resolution, i.e. exactly the LSP-based
approach already ruled out at scale in finding 1 below.

Root-caused further: the false positives weren't from direct lexical
imports (which import-aware static parsing could resolve) but from pytest
fixtures -- e.g. `test_dcm_cocoindex_search.py` calls `dcm_search.pattern_search_code(...)`
where `dcm_search` is a fixture (defined in `conftest.py`) bound to a specific
module at fixture-setup time, not an importable name a static parser could
trace back to one module. Import-aware resolution would have fixed nothing
here.

### Precision fix: signature-compatibility filtering + Graphify-style ambiguous-edge reporting

Compared against Graphify-Labs/graphify's own approach (also purely
syntactic, no type resolution): Graphify tags every edge EXTRACTED /
INFERRED / AMBIGUOUS and **skips** ambiguous matches outright, explicitly
choosing precision over recall rather than guessing. Adopted the same
policy here, via a filter that's still name/text-based (no new dependency,
no import/type resolution):

1. **Signature-compatibility filtering**: a same-named candidate is dropped
   if the call passes a keyword argument (`repo=`, `limit=`, etc.) the
   candidate's own parameter list doesn't accept (and it has no `**kwargs`
   catch-all). This is a generic filter, not special-cased to
   `pattern_search_code` -- it directly explains the fixture case above:
   `dcm_search.pattern_search_code(pattern, "go", repo="engram")`'s `repo=`
   keyword is only accepted by the five `*_search.py` modules' copies (each
   takes a `repo` param for multi-root scoping), not by
   `search/engram.py`'s single-root copy -- so the filter alone rules out
   exactly the false-positive target.
2. **Graphify's "one confirmed match or nothing" policy**: an edge is only
   added when filtering (same-file preference, then signature
   compatibility) narrows candidates to *exactly one*. Calls where 2+
   candidates remain are dropped from the graph but never silently -- they're
   recorded in `graph.graph["ambiguous_calls"]` (surfaced via
   `unresolved_calls`/`ambiguous_calls` counts in the `blast_radius` tool's
   output) so they're visible as "known-uncertain" rather than either
   corrupting the graph or vanishing without a trace.

Re-ran the same cross-check after the fix: `search/engram.py::pattern_search_code`
now resolves to exactly its 2 true-positive callers (`engram_code_pattern_search`,
`_run_cli_pattern_query` -- both same-file) and zero false positives; the 20
former false-positive edges are now reported as ambiguous (2-6 candidates
each) instead of confidently wrong. Whole-repo re-run: 1,417 edges (down
from the pre-fix count, as expected -- ambiguous fan-out edges no longer
silently added) and 585 calls now explicitly flagged ambiguous rather than
resolved to an arbitrary target. **Practical implication for consumers of
`blast_radius`**: precision on the demonstrated failure mode (duplicated
function names) is now high without sacrificing the recall measured above --
ambiguity is reported, not hidden, and not guessed at.

### Persistence go/no-go recommendation (for issue #43 phase 2)

**No persistence needed for `engram` itself** -- sub-1s live rebuild is fine
for interactive use. **Do not yet decide for kubernaut/koku/praxis** off
this data; repeat this same measurement on one larger repo (`koku`
recommended: smallest of the three, Python like engram so the same
extraction code applies unmodified) before committing to a Postgres/`graph.json`
persistence design.

## What CodePattern does and doesn't close

`CodePattern`/`match_code` (`cocoindex.ops.code`) is purely single-file/single-source syntactic tree-sitter shape matching — "find code shaped like X" via `\NAME`-style metavariables, no type resolution, no cross-file relationships. It fully closes the "shape search" half of what Graphify offers. It does **not** touch the other half: Graphify's actual differentiator is building a cross-file call graph via tree-sitter, then running Leiden community detection on it — a capability with no analog anywhere in this stack (`cocoindex_search` = similarity search, Hindsight's entity graph = single-hop co-occurrence over memory facts, Serena/gopls = one-symbol-at-a-time navigation).

## Feasibility findings (2026-08-24)

### 1. LSP-based (semantic) call-graph extraction is not viable at scale

Tested both candidate extraction approaches directly against `kubernaut` (2,927 Go files, 17,264 top-level functions — ground truth via `rg -c "^func "`):

| Approach | Test | Result |
|---|---|---|
| Semantic (Serena/gopls `find_referencing_symbols` per function) | Timed one real call | **~7 seconds/symbol** |
| Syntactic (CocoIndex `CodePattern`, whole-repo scan) | Timed one pattern query across all 2,927 files | **~0.4 seconds total** |

7s × 17,264 functions ≈ 33+ hours sequentially for one repo, and would repeatedly hit the same shared `gopls` daemon that already crash-looped twice in production the same day under load (see `docs/findings/2026-08.md`'s two 2026-08-23 Serena entries). This rules out true type-resolved call-graph construction. The only practical approach is syntactic, name-based extraction — the same technique Graphify itself uses, so this isn't accepting a worse approach than the tool being compared against, just its same known accuracy ceiling (no type resolution — e.g. two unrelated structs' same-named methods are indistinguishable to the graph).

### 2. Dependency footprint is smaller than the original deferral assumed

The original comparison's resource-cost concern named `graspologic`/Leiden specifically. `graspologic` pulls in `scipy`/`scikit-learn`/`numpy`. Skipping it and using `igraph` + `leidenalg` directly instead (checked via the PyPI JSON API, arm64 macOS wheels):

- `igraph` (C-extension, self-contained): 2.05 MB, no external system libs
- `leidenalg`: thin wrapper, depends only on `igraph`
- `networkx` core (no `numpy`/`scipy` "default" extras): 2.07 MB

Total: ~5–6 MB, not the heavier stack originally assumed.

### 3. Scale across the repos actually in use

| Repo | Language | Files | Functions |
|---|---|---|---|
| `kubernaut` | Go | 3,006 | 17,264 |
| `koku` | Python | 1,064 | 7,893 |
| `praxis-ai` | Rust | 320 | 6,806 |
| `praxis-grid` | Rust | 126 | 3,866 |

All in the thousands. The LSP-infeasibility finding above generalizes across all three languages actually in use here (Go, Python, Rust), not just Go — even the smallest (`praxis-grid`) would take ~7.5 hours at the measured per-symbol LSP rate.

### 4. Language coverage: no gap for the languages actually in use

Confirmed directly against the installed `cocoindex` package: `CodePattern` accepts `language="rust"`, `"python"`, and `"go"` natively (all three instantiate and match correctly), and `detect_code_language()` (already used elsewhere in this codebase) correctly maps `.rs` → `rust`. Graphify's broader multi-language coverage (its near-daily commit history is mostly per-language extraction bugfixes for Kotlin, C#, Ruby, Java, etc.) is chasing languages not used in this workspace at all. For the actual footprint — Go, Python, Rust — an engram-local extension would have full parity, with zero additional wiring risk beyond what the existing `*_code_pattern_search` tools already demonstrate.

## What we would and wouldn't be replicating from Graphify

| Graphify feature | Replicate? |
|---|---|
| Tree-sitter call-graph extraction | Yes — the core of this proposal |
| NetworkX + Leiden community detection | Yes — same technique |
| Query via MCP (blast radius, clusters, shortest path) | Yes, more tightly integrated (lands in the existing gateway, not a separate stdio server) |
| LLM-driven concept extraction from docs/PDFs/images | **No** — different capability, real LLM cost, overlaps with what Hindsight already does differently |
| Export to `graph.json`/HTML/Obsidian/wiki | **No, not initially** — aimed at human graph browsing, not the agent-queried MCP use case here; cheap to add later if ever wanted |
| Packaged as a multi-agent "skill" (Claude Code/Cursor/Codex/Gemini-CLI) | N/A — already MCP-native everywhere, so this solves a problem we don't have |

## Where this should live

**Engram-local CocoIndex extension** (a custom `@cocoindex.op.function()`/`@coco.fn`, CocoIndex's documented extension point — no core fork needed), not a CocoIndex core contribution, and not a standalone tool/service:

- Using the capability requires the `igraph`/`leidenalg` dependency regardless of who owns the code, so upstreaming to CocoIndex core wouldn't reduce our own resource-cost footprint.
- CocoIndex is a general-purpose ETL-for-AI framework; most of its user base isn't doing code intelligence, and Leiden clustering is a more opinionated, narrower addition than the existing `ops.code` precedent (`CodePattern`, broadly useful for any structural search). Upstreaming is worth reconsidering later, only if an engram-local version proves broadly useful and stabilizes — not before.
- A standalone tool (e.g. adopting Graphify itself) reintroduces the "yet another always-on MCP server/dependency surface" cost that the original 2026-08-07 `CodePattern` "add" decision explicitly avoided (recall: consolidating `koku`'s `gopls`-equivalent into CocoIndex instead of standing up a separate server was an explicit design goal at the time).

## Rough effort estimate

- **Spike on `engram` itself** (smallest repo, low risk, proves the extraction+clustering pipeline end-to-end): ~half a day, reusing existing pattern-search plumbing.
- **Production integration on `kubernaut`** (largest/highest-risk repo — needs package-qualified naming to reduce name-collision false edges, a new storage table, new MCP tools, tests per this repo's TDD convention): ~3–5 focused days.
- **Rolling out to the remaining repos** (`koku`, `dcm`, `praxis-*`, `engram` itself): ~0.5 day each once proven on `kubernaut`.

## Open risks / non-goals to keep visible in any implementation

- **Accuracy ceiling is inherent, not a bug**: name-based call resolution without type info produces false-positive edges for common method names shared across unrelated types/packages, and false negatives through interface/polymorphic dispatch. Any exposed tool (e.g. `blast_radius`) must document this ceiling explicitly — a wrong answer trusted as authoritative is worse than no answer.
- **Not incrementally updatable like the existing live CocoIndex flows**: a single file's rename can affect edges anywhere a name collides elsewhere in the graph, so this needs to run as a periodic/on-demand full-repo batch rebuild, not ride the existing cheap per-file-diff incremental model.
- **No concrete triggering need has surfaced yet.** This preflight exists so the decision is ready to execute quickly if/when a real blast-radius/call-graph question actually blocks work — not because one has yet. Building ahead of that need would repeat the exact pattern this repo's own findings log already flagged and corrected for once (`docs/findings/2026-08.md`, 2026-08-07, `entity_labels`/`CodePattern` capability-survey entry).
