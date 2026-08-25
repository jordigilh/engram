# Call-Graph Extraction + Clustering

Status: **spike landed on `engram` (2026-08-24), rollout complete: koku, rhdh-plugins, praxis, dcm, and kubernaut all done** -- extraction, clustering, and 3 MCP tools implemented and verified against real, live checkouts, cross-checked against Serena/gopls ground truth. Kubernaut additionally required a Postgres-backed cache (fingerprint-invalidated, no TTL) to stay interactive -- see Phase 5. See [issue #43](https://github.com/jordigilh/engram/issues/43) for the actionable summary and acceptance criteria; this document holds the detailed study and results so `docs/findings/2026-08.md` doesn't have to carry it in full. See "Spike results" below for what was actually measured on `engram`; "Multi-org rollout" below that for koku/rhdh-plugins/praxis/dcm/kubernaut; the sections after that are the original preflight, unchanged.

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

## Multi-org rollout (2026-08-24, same day)

Sequenced simplest-to-most-complex, one org at a time with a preflight/spike
gate (escalate below 90% confidence) before each: **koku (Python) → rhdh
(TypeScript) → praxis (Rust) → dcm (Go) → kubernaut (Go, latency-gated
last)**. `kubernaut` stays live-rebuild-per-query like every other org unless
its own preflight later shows rebuild latency is actually a bottleneck --
persistence is not being pre-built speculatively.

### Phase 0: extract the shared query/format layer

Before rolling out to a second org, `search/engram.py`'s call-graph helpers
(`_build_graph_with_timing`, `call_graph_blast_radius`/`_shortest_path`/
`_get_cluster`, their `_format_*` counterparts) moved into `callgraph.py`
itself as `build_call_graph_with_stats`, `resolve_node`, `query_*`/`format_*`.
Every org's MCP tools are now thin wrappers supplying only "which root(s)/
language" -- no per-org logic duplication, and no risk of the three orgs'
copies silently drifting apart over time.

### Phase 1: koku (Python)

Zero new language work -- same `python` `DefSpec`/call pattern as `engram`
itself, so this phase only proved the wrapper-wiring pattern, not new
extraction logic. Preflight against the real 635-file checkout: 2.9s build,
2,470 nodes / 3,145 edges, 0 crashes, 99.9% def-capture (the two apparent
misses were docstring example code that tree-sitter correctly does not treat
as a real definition) -- cleared the ≥90% gate cleanly, executed directly.
`search/koku.py` gained the same three MCP tools + CLI flags as `engram`'s.

### Phase 2: rhdh-plugins (TypeScript) -- new definition shapes, and a grammar-mismatch bug that looked like a match_code bug

Scoped to `workspaces/boost/` only (the one plugin directory this repo's
existing `rhdh_plugins.py` tooling already targets). TypeScript needed real
new extraction work, unlike the Python-to-Python Phase 1:

- **Three definition shapes, not one.** `callgraph.py` gained a
  `DefSpec`/`_DEF_SPECS` structure so a language can register *several*
  pattern+kind pairs instead of just one: `function` declarations
  (`function \NAME(\(A*\)) \BODY`, kind `function_declaration`), class
  methods (`\NAME(\(A*\)) \BODY`, kind `method_definition`), and
  arrow-function consts (`const \NAME = \BODY`, kind `lexical_declaration`)
  -- the dominant React-component style in this codebase.
- **The arrow-const pattern over-matches** every `const`: plain data
  literals, factory-call results, and -- not anticipated up front --
  destructuring assignments like `const { t } = useTranslation()`, where
  tree-sitter's `NAME` capture binds the literal `{ t }` text rather than an
  identifier. A two-part filter, `_is_arrow_function_const()` (`NAME` must be
  a real identifier; `BODY` must start like `(...) =>`, `async (...) =>`, or
  a bare-identifier `=>`), narrows this to real components: 100%
  precision/recall measured against the real ~150-file corpus (the one
  apparent miss traced to the *measurement* script, not the extractor --
  `SidebarContent = NavContentBlueprint.make({...})` is a factory call,
  correctly excluded).
- **A trailing `\BODY` capture gets an exact body-end line for free** on all
  three TS patterns, since tree-sitter treats a brace-delimited block (or an
  arrow function's whole node) as one child span -- a real improvement over
  Python's indentation-approximated `_approximate_body_end_line`, which
  exists only because Python's body isn't a single bracketed AST node.
- **The interesting bug: a real arrow-const component vanished from
  extraction**, with symptoms indistinguishable from a `match_code` parser
  bug -- bisection isolated a 6-line repro (a multi-line ternary with an
  arrow-function branch inside a JSX attribute's `{...}` expression
  container) that made every *subsequent* arrow-const match in the file
  disappear, and this reproduced even against `cocoindex`'s own Rust
  `code_match` crate directly... except that direct reproduction used the
  `tsx` grammar and *passed*. Rerunning with the `typescript` grammar instead
  (matching what this investigation -- and this file's own long-standing
  `--language typescript` CLI examples -- had been using for `.tsx` content
  throughout) reproduced it immediately, with a `kind=ERROR` root node. The
  bug was never in CocoIndex: `tree-sitter-typescript` ships two distinct
  grammars, `typescript` (no JSX support) and `tsx` (JSX-aware);
  `detect_code_language()` already returns the correct one per extension and
  always has. Added `callgraph._grammar_for_path()` (resolves the real
  per-file grammar via `detect_code_language`, decoupled from the `language`
  config key used to select patterns) so every `call_graph_*` tool now gets
  this automatically. The same latent mismatch already existed in
  `rhdh_plugins.py`'s pre-existing `pattern_search_code`/CLI (`--language`
  defaulting to `"typescript"` silently skips every `.tsx` file -- most of
  this codebase's real components); documented prominently in both the
  function's and its MCP tool's docstrings rather than changed as a default,
  since no single default is correct across a tree mixing `.ts` and `.tsx`.

**End-to-end verification against the live `rhdh-plugins/workspaces/boost`
checkout**: 3.75-6.5s build, 373 nodes / 57 edges, 254/329 calls unresolved
(external library calls -- React/MUI/etc. -- same order of magnitude as
Python's stdlib-call baseline), 17 ambiguous. One scope boundary confirmed
by this run, not a bug: JSX component composition (`<Foo prop={x} />`) is a
`jsx_element`/`jsx_self_closing_element` AST node, not a `call_expression`,
so the call graph correctly does not create an edge for it -- only real
function/hook calls do (e.g. a component's body calling `useAiAssets()`
does create an edge, confirmed via `--cluster useAiAssets` correctly
grouping the calling component with the hooks it actually invokes).

Coverage: 27 new tests in `tests/test_rhdh_plugins_cocoindex_search.py`
(previously nonexistent for this module -- also added the missing
`rhdh_plugins_search` conftest fixture) plus 18 new TS-specific tests in
`tests/test_callgraph.py` (`TestIsArrowFunctionConst`, `TestGrammarForPath`,
`TestExtractDefinitionsTypeScript`, `TestExtractCallSitesTypeScript`). 817
tests total repo-wide, `ruff check` clean.

**Correction (found during Phase 3's preflight, same investigative pattern
applied to a new language): this section originally understated a real gap.**
A TypeScript function/method with an explicit return-type annotation
(`function f(): number { .. }`) got a *wrong*, not merely missing,
`body_end_line` -- silently corrupting caller attribution for every call
inside such a function -- because the `function_declaration`/
`method_definition` `DefSpec`s above had no return-type-aware pattern
variant. This was live in this codebase from the moment Phase 2 shipped
until the Phase 3 (Rust) preflight below re-exercised the same `\BODY`-
capture mechanism against a different language and caught the general
failure mode. Root cause, fix (return-type/generic-aware pattern variants
plus a `_looks_like_real_block_body` guard), and full explanation are in the
Phase 3 section below since the fix is one shared change covering both
languages -- see `docs/findings/2026-08.md`'s Phase 3 entry for the
narrative. Not a regression in anything users had already queried
successfully; caught before it was ever reported as a symptom.

### Phase 3: praxis-proxy (Rust) -- a real BODY-misattribution bug (not just missing coverage), multi-repo aggregation, and Rust's `.`/`::` split

Two things made Rust genuinely different from the previous two phases, not
just "port the pattern to a new grammar":

**1. `praxis.py` searches seven independent repo checkouts in one call**
(`praxis`, `praxis-ai`, `praxis-demos`, `praxis-forge`, `praxis-grid`,
`praxis-operator`, `praxis-policy` -- `_PATTERN_SEARCH_ROOTS`), unlike
`koku.py`/`rhdh_plugins.py`'s single root each. `build_call_graph()` gained
an optional `repo_tag` param that prefixes every `display_path` as
`f"{repo_tag}/{rel_path}"`, and a new `build_multi_repo_call_graph()`/
`_with_stats()` pair walks each repo independently (own `name_index`, own
signature filtering) and merges the resulting graphs by node/edge union --
deliberately **not** a single combined walk: a call in one repo can never
resolve to a same-named function in a different repo, matching the
same-file-preference precedent already established for same-repo
resolution (see the original spike's precision-fix section above). This is
generically reusable, not praxis-specific -- `dcm.py`'s upcoming Phase 4
has the identical eight-separate-repos shape.

**2. Rust needed the same {generic, return-type} pattern-arity split
already known from TypeScript, but pushed one investigation further and
surfaced a bug that had been silently live in the *already-shipped* Phase
2.** Preflighting `fn \NAME(\(A*\)) \BODY` against real generic/`pub`/
`async`/trait-impl Rust functions found the same "a literal `<`/`>` only
matches a def that has type params/lifetimes, never one that doesn't" split
already known from TS -- so `_DEF_SPECS["rust"]` initially mirrored TS's
two-pattern shape. But testing that pair against **realistic Rust, which
almost always has an explicit return type** (`fn f() -> T { .. }`) surfaced
something new: the no-return-type pattern didn't just skip a return-typed
fn, it *matched* it, with `\BODY` silently bound to the literal `"->"`
token instead of the real block -- corrupting `body_end_line` to equal the
def's own start line, not merely omitting the def. Checking whether the
already-shipped TS patterns had the identical exposure (they share the same
underlying `\BODY`-after-optional-field mechanism) confirmed they did:
`function f(): number { .. }` similarly bound `\BODY` to `": number"`. This
had been live since Phase 2 shipped, undetected because it doesn't produce
an exception or an obviously-wrong result in isolation -- just a corrupted
`body_end_line`, silently breaking caller attribution for every call inside
such a function's real body. Fixed once, for both languages: every
brace-bodied `DefSpec` now needs the full 2x2 {generic, return-type}
combination (Rust: 4 patterns for its one `function_item` kind; TypeScript:
4 each for `function_declaration` and `method_definition`, 9 total with the
unaffected arrow-const pattern), plus a new universal `extract_definitions`
guard (`_looks_like_real_block_body`: a real block, in every brace-delimited
language here, starts with `{` once stripped -- no return-type annotation
ever does) that discards whichever wrong-arity pattern's capture doesn't
actually look like a block, alongside an `_IDENT_RE` check on `NAME` (a
wrong-arity match was also observed capturing garbage like literal `"<T>"`
as the name). Verified: the correct-arity twin's capture always survives,
so a def with N applicable specs yields exactly 1 `FunctionDef`, never 0 or
2+. One further axis was found and *not* chased: Rust `where` clauses
(`fn f<T>(x: T) -> T where T: Clone { .. }`) sit between the return type and
body, a third arity dimension beyond {generic, return-type} -- measured at
~0.37% of function-shaped lines across the real praxis-proxy checkouts (113
`where` lines / ~30,319 fn-shaped lines), so `_looks_like_real_block_body`
correctly drops these defs entirely (safe, not silently wrong) rather than
adding a third combinatorial dimension for that little recall.

**3. Rust's call-site resolution needed one targeted fix**: `\NAME(\(A*\))`
correctly captures method calls (`self.bar()`/`f.bar()`, NAME text
`"self.bar"`/`"f.bar"`) the same way TS captures member-expression calls,
but Rust's associated-function/path-qualified call syntax uses `::`, not
`.` (`Foo::new()`, `scoping::score_backends()`) -- the existing bare-callee
trailing-segment split (`callee_name.rsplit(".", 1)[-1]`) left `"Foo::new"`
completely unsplit (no literal `.` in it), so it could never resolve
against `name_index`. Fixed by normalizing `::` to `.` before the split
(`callee_name.replace("::", ".").rsplit(".", 1)[-1]`) -- confirmed against
real code, not just the synthetic regression test: `praxis-grid`'s
`routing_overlay.rs::provider_ordering_scores` calls
`scoring::score_backends(..)`, and `blast_radius` for `score_backends`
correctly lists it as a depth-1 caller.

**End-to-end against all seven live praxis-proxy checkouts**: 33s build
(the slowest org so far -- 7 repos vs. 1, and Rust's heavier per-file
pattern-matching cost), 23,786 nodes / 41,151 edges, 77,398/155,941 calls
unresolved (external-crate calls, same category as the other orgs'
stdlib/library-call baseline), 29,368 ambiguous (Rust's high rate of
same-named trait/inherent methods -- `new`, `get`, etc. -- compounded by
seven repos never cross-resolving). 33s is noticeably slower than koku's
2.9s or rhdh-plugins' ~6s; not treated as a persistence trigger per the
existing policy (only `kubernaut`'s own preflight decides that), but worth
watching if it becomes a real interactive-UX complaint for praxis
specifically.

Coverage: 14 new Rust/multi-repo tests in `tests/test_callgraph.py`
(`TestExtractDefinitionsRust`, `TestExtractCallSitesRust`,
`TestBuildCallGraphRust`, `TestBuildMultiRepoCallGraph`), 4 new TS
regression tests closing the Phase 2 gap found above
(`test_return_typed_function_declaration_gets_correct_body_end_line` and 3
siblings), and 7 new wiring tests in `tests/test_praxis_cocoindex_search.py`
(`TestCallGraphWiring`, multi-repo-aware). 842 tests total repo-wide, `ruff
check` clean.

### Phase 4: dcm (Go) -- the predicted {generic, return-type} split confirmed, minus one axis for methods, plus `repo=` scoping

Go's preflight (`func \NAME(\(A*\)) \BODY` and its method twin
`func (\(R*\)) \NAME(\(A*\)) \BODY`, tested against plain/return-typed/
generic/method variants before writing any `_DEF_SPECS` entries, per the
preflight-before-each-phase policy) confirmed the exact bug class predicted
from the Phase 2/3 writeup above, with two Go-specific wrinkles:

**1. Same {generic, return-type} `\BODY`-misattribution bug, Go's own
syntax for both axes.** A return-typed Go func (`func f() error { .. }`,
or the multi-value form `func f() (int, error) { .. }`) matched the
no-return-type pattern with `\BODY` bound to the return-type text itself
(`"error"`, `"(int, error)"`) instead of the real block -- identical
failure mode to Rust's `->`/TS's `:`, just with Go's zero-separator
juxtaposition (`func \NAME(\(A*\)) \RET \BODY`, no token between the
parameter list and the return type at all). Go generics
(`func Generic[T any](x T) T { .. }`) use `[T any]`, not `<T>` --
`_DEF_SPECS["go"]` uses a literal `[`/`]` pair the same way TS/Rust use
`<`/`>`, and the plain pattern with no return type didn't even match a
generic+return-typed func at all until the `func \NAME[\(G*\)](\(A*\)) \RET
\BODY` twin was added, mirroring the exact 2x2 arity split from Phase 2/3.
Named return values (`func f() (result int, err error) { .. }`) are just
another case of the same return-type axis, not a third one -- the existing
`\RET` capture handles them with no extra pattern needed. The existing
`_looks_like_real_block_body`/`_IDENT_RE` guards (unchanged, fully
generic already) caught every wrong-arity match with zero Go-specific
code -- confirming they really are the general fix, not something
Rust/TS-specific that happened to also need porting.

**2. One fewer axis for `method_declaration` than `function_declaration`.**
Go methods cannot have their own type parameters -- only the receiver type
can be generic (`func (s *Server[T]) Method(x T) T { .. }`), and that
generic is already fully inside the existing `\(R*\)` receiver capture, so
the plain `func (\(R*\)) \NAME(\(A*\)) \BODY` pattern (no bracket literal
of its own) matched a generic-receiver method correctly on the first try
with no separate spec needed. Verified directly (not just inferred from
the grammar) before writing `_DEF_SPECS["go"]`, since assuming this without
checking would have been exactly the kind of "should work" guess the
preflight policy exists to catch. Net result: 4 patterns for
`function_declaration` (the full 2x2), but only 2 for `method_declaration`
(the return-type axis alone) -- 6 total, the smallest per-language spec
count of any language so far. A bodyless interface method signature is a
different kind (`method_elem`, not `method_declaration`), naturally
excluded by the kind filter, same pattern as Rust's
`function_signature_item`.

**3. No call-site fix needed.** Go uses only `.` for both package-qualified
(`pkg.Func()`) and method (`obj.Method()`) calls -- no `::` operator like
Rust -- so the existing bare-callee trailing-segment split needed no
changes. Confirmed: `int64(x)`-style type-conversion calls are captured as
ordinary `call_expression` nodes with the type name as `NAME` (matching
`String(x)`/`int(x)` in TS/Python), correctly falling into the existing
unresolved-call bucket rather than needing special-casing.

**4. `dcm.py` reuses `build_multi_repo_call_graph_with_stats` across its 8
independently-checked-out Go repos**, the identical multi-repo shape
`praxis.py` established in Phase 3 (confirmed reusable exactly as
predicted there, not just "the same idea ported") -- `_PATTERN_SEARCH_ROOTS`
already existed in the right `(repo_tag, root, included, excluded)` shape
since `pattern_search_code` already used it. One deliberate addition beyond
the Phase 3 precedent: dcm's 3 new call-graph functions/MCP tools all
accept an optional `repo=` parameter, mirroring `pattern_search_code`'s own
existing `repo` scoping (praxis.py has no such param -- it always searches
all seven Rust repos). Justified by dcm having one more repo than praxis
(8 vs. 7) and most blast-radius/cluster questions in practice already being
scoped to a repo the caller knows, trading a slower default (no filter) for
a fast, common case (`repo="dcm-cli"` etc.) without adding a second
code path.

**End-to-end against all 8 live dcm-project checkouts**: ~9s steady-state
build (2 consecutive runs: 8.87s cold, then 9.12s/8.65s), 3,176 nodes /
3,202 edges, 15,533/22,484 calls unresolved (Go's larger stdlib +
Cobra/Viper/GORM dependency surface than koku's or rhdh-plugins', same
external-call category as every other org), 2,904 ambiguous. Ground-truth
spot-checked two ways: (a) a bare-name query for `main` across all 8 repos
correctly reported all 10 same-named `main` functions (dcm-three-tier-sp
alone contributes 2, via a vendored nested checkout) as ambiguous
candidates rather than guessing one; (b) a real 2-hop production chain,
`cmd/control-plane/main.go::main` calls `internal/app/run.go::Run` calls
`internal/sp/service/provider/provider.go::NewProviderService`, resolved
exactly right by `blast_radius` (queried from `NewProviderService`'s side:
depth 1 = `Run`, depth 2 = `main`) and was independently confirmed via
`grep` against the live checkout (`run.go:98` calls
`spprovidersvc.NewProviderService(..)`, `main.go:10` calls `app.Run()`).

Coverage: 10 new Go tests in `tests/test_callgraph.py`
(`TestExtractDefinitionsGo`, `TestExtractCallSitesGo`,
`TestBuildCallGraphGo`), and 11 new wiring tests in
`tests/test_dcm_cocoindex_search.py` (`TestCallGraphWiring` plus 3
`repo=`-aware CLI-routing additions to `TestMainRouting`). 865 tests total
repo-wide, `ruff check` clean.

### Phase 5: kubernaut (Go) -- the one repo that broke the rebuild-every-call budget, fixed with a Postgres cache instead of a new component

Every prior phase reused the plain, uncached
`build_multi_repo_call_graph_with_stats` -- a full rebuild on every call,
same as `pattern_search_code` already does, deliberately: no invalidation
logic to get wrong, always-fresh results, and every measured build stayed
comfortably interactive (dcm's 8-repo build ~9s, praxis's 7-repo build in
the same ballpark). Preflighting kubernaut against that same plan broke the
pattern: kubernaut's own repo alone (1,053 non-test/non-vendor Go files)
took **55.4s** to build, an interactive-tool latency no other org came
close to. `kubernaut-operator` (36 files) is trivial by comparison (~1.4s)
and was never the problem.

**Investigating the bottleneck.** `extract_definitions` calls `match_code`
once per `_DEF_SPECS` pattern per file -- Go's spec table is 6 patterns (4
for `function_declaration`, 2 for `method_declaration`, the smallest
per-language count per Phase 4 above, but still 6x the single-pattern-per-
file cost of a hypothetical one-pattern language), so kubernaut's file count
alone predicts a worse multiplier than dcm's 8-repos-but-mostly-smaller
mix. A `ThreadPoolExecutor`-based parallelization of the per-file extraction
loop was spiked and got the full build down to ~25.5-29s -- a real 2x, but
still not comfortably interactive, and it only addresses the *cold* case; a
tool a user calls repeatedly against an unchanged tree still pays that cost
on every single call under the rebuild-every-time policy. That's a policy
problem, not (only) a performance one -- worth fixing at the caching layer
rather than continuing to chase constant-factor speedups on every rebuild.

**Design decisions, driven directly by user constraints during this
phase:**

1. **Cache backend: Postgres, not a new service.** Valkey (Redis-compatible,
   TTL support out of the box) was the first design considered and was
   partially implemented (dependency added, local `valkey-server` started,
   a `valkey==6.1.1` entry landed in `pyproject.toml`) before being reverted
   in favor of Postgres, once the user raised "I'd rather avoid adding new
   components" / "we already have 3 + postgres" (referring to the launchd
   services + Postgres this stack already runs). Postgres is already a hard
   dependency of every `*_search.py` module (`psycopg2`, `COCOINDEX_PG_URL`)
   and already running -- a new `cocoindex.call_graph_cache` table (`cache_key
   TEXT PRIMARY KEY, fingerprint TEXT, graph_data BYTEA, updated_at
   TIMESTAMPTZ`) reuses that connection with zero new components to
   install, run, or monitor. `_ensure_cache_table()` issues
   `CREATE SCHEMA IF NOT EXISTS`/`CREATE TABLE IF NOT EXISTS` itself (once
   per process, via a module-level ready flag) rather than depending on a
   separate migration step.
2. **No TTL -- fingerprint-only invalidation.** A fixed expiry would force a
   rebuild of an *unchanged* tree on some arbitrary schedule (wasted work on
   a quiet weekend) while still not guaranteeing a just-before-expiry edit
   gets caught promptly. Instead, every cache read/write is keyed on a
   content fingerprint -- file count + max mtime across the exact same
   `chunking.find_code_files`-walked file set the real build itself walks
   (`compute_fingerprint()`) -- computed fresh on every call and compared
   against the stored one. A hit requires an exact fingerprint match; any
   mismatch (or missing row) triggers a real rebuild and an upsert of the
   new fingerprint + pickled graph. A quiet tree never rebuilds no matter
   how much wall-clock time passes; an edit is caught on the very next call,
   not on some cache's next scheduled expiry.
3. **Graceful fallback, not a hard dependency.** Any Postgres error --
   unreachable, corrupted row (a failed `pickle.loads`), a write failure
   after a real rebuild -- is caught and logged, falling back to (or simply
   returning the result of) an uncached rebuild rather than failing the
   tool call. This is exactly the pre-Phase-5 behavior every other org
   already has, so a database hiccup degrades kubernaut's call-graph tools
   to "slower," never "broken."
4. **Scope: Go only, main branch only, kubernaut's original two repos.**
   `_CALL_GRAPH_ROOTS` in `kubernaut.py` is deliberately narrower than the
   pre-existing `_PATTERN_SEARCH_ROOTS` (which also covers
   `kubernaut-console` TypeScript and `@release-vX.Y` mirrors) -- the same
   "don't chase every remaining axis" call already made for Rust's `where`
   clauses in Phase 3. `kubernaut`/`kubernaut-operator` were this whole
   spike's original motivating target; TypeScript and release-line
   call-graph coverage stay explicit, documented gaps rather than silent
   omissions.
5. **Memory footprint was checked, not assumed.** The user's core concern
   throughout this phase was host memory pressure from a persistent cache
   on a shared host, not raw latency. `tracemalloc` measurement of the
   in-process graph object put the *net* live allocation at ~735 bytes/node
   -- kubernaut+kubernaut-operator's combined 9,391 nodes extrapolate to
   ~6-7MB, confirming the graph object itself was never the scaling risk
   (an earlier ~80MB RSS delta observed during ad hoc measurement was
   transient parsing/import overhead, not the graph's steady-state
   footprint). This finding informed but didn't change the final decision:
   Postgres was chosen for "no new component," not because Valkey's memory
   cost was disqualifying on its own.

**Implementation** (`src/engram/callgraph.py`): `compute_fingerprint()`,
`_ensure_cache_table()`, and
`build_multi_repo_call_graph_with_stats_cached()` -- the last is the new
public entry point, taking the same `(roots, language)` shape as
`build_multi_repo_call_graph_with_stats` plus a `cache_key` (so multiple
independently-cached queries, e.g. per-`repo=` scope, don't collide) and a
`pg_url`. Wired into `kubernaut.py` only: `_build_graph_with_timing(repo=None)`
selects from `_CALL_GRAPH_ROOTS` and builds a `repo`-aware cache key
(`"kubernaut-go:all"` or `"kubernaut-go:<repo>"`), and the three
`call_graph_*` functions + `cocoindex_call_graph_*` MCP tools + `--blast-radius`/
`--shortest-path`/`--cluster` CLI flags mirror dcm.py's Phase 4 `repo=`-scoped
shape exactly, substituting the cached builder for the plain one.

**End-to-end against the live checkouts** (`~/.hindsight/watch/kubernaut` +
`~/.hindsight/watch/kubernaut-operator`, cache table dropped and rebuilt
fresh for this run): cold build (both repos, cache MISS) **56.70s**, 9,391
nodes / 10,339 edges, 38,765/66,225 calls unresolved, 15,087 ambiguous
(Go's stdlib + the platform's own large cross-controller call surface, same
external-call category every prior phase has seen, at kubernaut's larger
scale). A second, immediately-following call against the identical
`Reconcile`-implementer query was a cache **HIT** and completed in **0.71s
wall** (including Python interpreter startup) -- roughly an **80x** speedup
over the cold path, comfortably interactive. `repo="kubernaut-operator"`
scoping was confirmed to use an independent cache key
(`"kubernaut-go:kubernaut-operator"`, 597 nodes, built fresh in 1.36s) that
does not collide with the unscoped `"kubernaut-go:all"` entry. Invalidation
was verified against real file mtimes, not just simulated: `touch`-ing a
live `kubernaut-operator` source file caused the very next
`repo="kubernaut-operator"`-scoped call to report a cache MISS and rebuild
(new fingerprint, new `updated_at`), while the unrelated `"kubernaut-go:all"`
row's fingerprint (which also covers that same file, being a superset)
was independently confirmed stale on the next full-scope call too --
correct behavior, no manual invalidation step needed either way. A
same-bare-name (`Reconcile`) blast-radius query correctly reported all 14
same-named implementers across both repos as ambiguous candidates rather
than guessing one, exercising the identical multi-candidate resolution
behavior verified for dcm's `main` in Phase 4.

Coverage: 9 new cache tests in `tests/test_callgraph.py`
(`TestComputeFingerprint`, `TestBuildMultiRepoCallGraphWithStatsCached` --
hit/miss/fallback/corrupt-row/write-failure/multi-key-isolation, all mocking
`psycopg2` directly) plus 4 new integration tests in
`tests/integration/test_callgraph_cache_it.py` (real round-trip against the
disposable-container `pg_url` fixture already established for pgvector
search testing -- create-on-first-use, cache hit, file-change invalidation,
unreachable-Postgres fallback) and 15 new wiring tests in the new
`tests/test_kubernaut_cocoindex_search.py` (`TestCallGraphWiring` -- an
autouse fixture forces every test onto the Postgres-unreachable fallback
path so wiring correctness stays independent of cache correctness, already
covered elsewhere -- plus `TestMainRouting` for the 3 new CLI flags). 884
tests total repo-wide (`-m "not integration"`), `ruff check` clean.

The Valkey path explored earlier in this phase (client dependency,
`brew services` entry, `pyproject.toml` line) was fully reverted before
landing this design -- see this section's point 1 above for why Postgres
was chosen instead once the "avoid new components" constraint was made
explicit.

#### Same-day follow-up: making the cache branch-aware (main + release/v1.5, extensible to v1.6)

The first Phase 5 land (above) deliberately scoped `_CALL_GRAPH_ROOTS` to
main-branch-only, mirroring the "don't chase every remaining axis" calls
made in earlier phases. That scope didn't survive contact with how
kubernaut-family workspaces actually work: **every workspace is a dedicated
clone targeting one specific branch** (main, or a `release/vX.Y` line --
currently `v1.5`, the newest cut; `v1.6` is pre-provisioned in
`KUBERNAUT_RELEASE_LINES` but has no live mirror yet), and
`search_code()`/`pattern_search_code()` already had to solve exactly this
problem back on 2026-08-10 (see that section of `docs/findings/2026-08.md`)
-- a main-only call graph would have silently served the wrong branch's
code (or a false "not found") to every v1.5 workspace, and worse, a
naive cache-key fix alone (without also fixing the *build* scope) would
have made a v1.5 workspace's first query poison the shared cache with
main's stale code under a key a later main workspace would then trust.

**Fix: reuse the existing branch-detection machinery end to end, not just
patch the cache key.** `_CALL_GRAPH_ROOTS` gained the identical
`@release-vX.Y` mirror-extension loop `_PATTERN_SEARCH_ROOTS` already had
(same `_release_line_dir()` convention, same `KUBERNAUT_RELEASE_LINES` env
var), restricted to the two Go repos (`kubernaut-console`/TypeScript stays
out of call-graph scope, unchanged from the original Phase 5 decision). A
new `_select_call_graph_roots(repo, release_line)` mirrors
`_select_pattern_roots` exactly. `_build_graph_with_timing()` now takes a
`branch` parameter and resolves it through the *same*
`_resolve_release_line()` every other branch-aware function here already
uses -- explicit override wins, else auto-detect from
`KUBERNAUT_LIVE_CLONE_DIR` (which live checkout this MCP server instance
was spawned alongside), `"main"` forces main regardless of checkout. All
three `call_graph_*` functions, all three `cocoindex_call_graph_*` MCP
tools, and the `--blast-radius`/`--shortest-path`/`--cluster` CLI flags
gained the matching `branch=`/`--branch` parameter.

**The cache-key fix that actually makes this safe**: `cache_key` folds in
the *resolved* release line, not the raw `branch` argument
(`f"kubernaut-go:{repo or 'all'}:{release_line or 'main'}"`) -- so an
explicit `branch="v1.5"` call and an auto-detected v1.5-checkout call share
one cache entry (correct: same code, same answer), while a main call and a
v1.5 call always get distinct entries (`kubernaut-go:all:main` vs.
`kubernaut-go:all:v1.5`) even when both happen to resolve through
auto-detection. Without folding in the *resolved* value, two workspaces on
the same branch reached via different `branch=`/auto-detect paths could
have missed each other's cache entirely (wasted rebuilds, but not
incorrect); the actual correctness risk this specifically prevents is a
release-line's first cache write silently overwriting main's entry (or
vice versa) if both had reduced to the same key.

**End-to-end against the live `kubernaut-release-v1.5` +
`kubernaut-operator-release-v1.5` mirrors** (880 + 27 Go files): cold build
`branch="v1.5"` **61.68s**, 7,634 nodes / 7,503 edges, 39,979/66,865 calls
unresolved, 17,372 ambiguous -- correctly built entirely from
`@release-v1.5`-tagged nodes (verified: every `Reconcile` candidate in the
ambiguous-match output carries the `@release-v1.5` tag, none of main's
untagged paths). The identical query repeated was a cache **HIT** at 0.76s
wall. A fresh `branch="main"` query in between (forcing a rebuild via a
distinct qualified function name) confirmed `cocoindex.call_graph_cache`
ends up with three independent, non-colliding rows after all three query
shapes exercised in this phase: `kubernaut-go:all:main`,
`kubernaut-go:all:v1.5`, and the earlier `repo="kubernaut-operator"`
scoped-build test's own key -- each with its own fingerprint, never
overwriting another. (Two now-orphaned rows using the pre-this-fix
`kubernaut-go:all`/`kubernaut-go:kubernaut-operator` key format, created by
the original Phase 5 land's manual e2e run before this follow-up existed,
were deleted -- a real production instance would simply accumulate one
stale, never-hit row per pre-fix key rather than requiring manual cleanup,
since the fingerprint check alone can't reconcile an old key format to a
new one.)

Coverage: 6 new wiring tests in `tests/test_kubernaut_cocoindex_search.py`'s
new `TestCallGraphBranchScoping` class (explicit branch, default/auto-detect,
`branch="main"` override, unrecognized-branch fallback, no cross-branch
resolution, and two tests spying on the exact `cache_key` string passed to
`callgraph.build_multi_repo_call_graph_with_stats_cached` to pin down the
branch-isolation and repo+branch-combination behavior directly rather than
inferring it from build outcomes alone) plus 2 updated
`TestCallGraphWiring` tests and 2 updated `TestMainRouting` tests
(`branch` now appears in every CLI-routing assertion). 893 tests total
repo-wide (`-m "not integration"`), `ruff check` clean.

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
