# Call-Graph Extraction + Clustering — Design Reference

This is the **"how it works" reference**: the extraction mechanism, call-resolution
algorithm, multi-repo aggregation, clustering, and caching architecture, as a
standalone read independent of how any of it was discovered.

For the chronological story — the spike, the bugs found, per-language findings,
precision measurements, and per-org rollout numbers — see
[`docs/CALL_GRAPH_CLUSTERING.md`](CALL_GRAPH_CLUSTERING.md) instead. For setup,
running modes, and operational concerns (caching setup, branch scoping), see
[`docs/COCOINDEX.md`'s Call-Graph Queries section](COCOINDEX.md#call-graph-queries).

All of this lives in one module: `src/engram/callgraph.py`.

## What it answers

Three relational questions no other tool in this stack answers (hybrid search
answers "find code *about* X"; `CodePattern`/`*_code_pattern_search` answers
"find code *shaped like* X" within one file at a time):

- **Blast radius** — who (transitively) calls this function, up to N hops:
  "what breaks if I change this."
- **Shortest path** — does A ever reach B through a chain of calls, and how.
- **Clustering** — which group of related functions does X naturally belong
  to (Leiden community detection over the call graph).

## Pipeline overview

```
source files
    │  (chunking.find_code_files — same file-walk every *_code_pattern_search
    │   tool already uses)
    ▼
extract_definitions()  ──┐
    │  (per-file, via                 │  DefSpec-driven, one or more
    │   cocoindex's match_code)       │  tree-sitter patterns per language
    ▼                                 │
extract_call_sites()                  │
    │  (per-file, containment-based   │
    │   caller attribution)           │
    ▼
build_call_graph()  →  resolve every call, one repo at a time
    │  (name-index + same-file preference + signature-compatibility
    │   filtering + "exactly one candidate or nothing" edge policy)
    ▼
[build_multi_repo_call_graph() for multi-repo orgs — union-merge, never
 cross-resolves between repos]
    │
    ▼
nx.DiGraph  →  compute_clusters() (Leiden)
    │
    ▼
[build_multi_repo_call_graph_with_stats_cached() — kubernaut only,
 Postgres-backed, fingerprint-invalidated]
    │
    ▼
query_blast_radius() / query_shortest_path() / query_get_cluster()
    │
    ▼
format_*_result()  →  MCP tool response
```

## 1. Extraction: `CodePattern`/`match_code`, no new tree-sitter dependency

Extraction is built entirely on CocoIndex's existing `match_code()` — the same
by-example tree-sitter pattern matcher every `*_code_pattern_search` MCP tool
already uses. No new dependency was added to detect functions or calls: the
same bare pattern shape (e.g. `\NAME(\(A*\))`) matches both a function
definition's header and a call expression; `CodeMatch.kind`
(`"function_definition"` vs. `"call"`, or their per-language equivalents)
distinguishes which one a given match actually is.

### `DefSpec`: one or more pattern+kind shapes per language

A single tree-sitter pattern per language isn't enough — most languages have
several syntactically distinct ways to define a function, and (for
brace-bodied languages) generic/return-type annotations change which literal
pattern is needed to match correctly at all. `DefSpec` captures one
pattern+kind(s) shape:

```python
@dataclasses.dataclass(frozen=True)
class DefSpec:
    pattern: str
    kinds: frozenset[str]
    extra_filter: Callable[[str, str], bool] | None = None
```

`_DEF_SPECS` registers however many `DefSpec`s each language needs:

| Language | # specs | Why |
|---|---|---|
| Python | 1 | One shape (`def \NAME(\(A*\))`, no trailing `:` — see below) |
| TypeScript | 9 | 4 for `function` declarations × {generic, return-typed} axes, 4 for class methods, 1 for arrow-function consts (`const Foo = (...) => {...}`) |
| Rust | 4 | `fn` × {generic, return-typed} axes (covers free fns, impl methods, trait-impl methods alike — modifiers/enclosing blocks are separate AST nodes) |
| Go | 6 | 4 for `function_declaration` × {generic, return-typed}, 2 for `method_declaration` (return-type axis only — a method's type params live entirely in its receiver, already inside the `\(R*\)` capture) |

**Why the generic/return-type split is mandatory, not just extra coverage:**
a `DefSpec` pattern missing an annotation placeholder that a real definition
*has* doesn't just fail to match — it still matches, with its `\BODY` capture
silently bound to the annotation's own text (e.g. the literal `"-> T"` for
Rust, `": T"` for TypeScript) instead of the real block. That produces a
`FunctionDef` with a corrupted `body_end_line` (often equal to its own
`start_line`) rather than no entry at all — silently breaking caller
attribution for every call inside that function, without raising anything.
This was found live in already-shipped code (see
[`CALL_GRAPH_CLUSTERING.md`'s Phase 3](CALL_GRAPH_CLUSTERING.md#phase-3-praxis-proxy-rust----a-real-body-misattribution-bug-not-just-missing-coverage-multi-repo-aggregation-and-rusts--split)),
not caught in review — hence the two universal guards below.

**Two universal guards**, applied in `extract_definitions` regardless of
which `DefSpec` matched:

1. **`NAME` must be a real identifier** (`_IDENT_RE`) — a spec applied to a
   definition shaped for a *different* spec can still "match" with `NAME`
   bound to some unrelated fragment (observed: the literal text `"<T>"`).
2. **A `BODY` capture must look like a real block** (`_looks_like_real_block_body`
   — starts with `{` once stripped) unless the spec has its own
   `extra_filter`. Every real block body in every brace-delimited language
   here starts with `{`; no return-type annotation text ever does.

Together, exactly one of a definition's applicable specs survives both
guards — by construction, since the generic/return-type axes are mutually
exclusive per real definition.

**Python has no trailing `:`** in its pattern (`def \NAME(\(A*\))`, not
`def \NAME(\(A*\)):`) — discovered mid-spike that the colon-terminated version
silently fails to match any function with a return-type annotation, since the
annotation sits between the parameter list's closing paren and the colon.
Python also has no `\BODY` capture at all (its body isn't a single bracketed
node `match_code` can capture as one unit), so its body end is
*approximated* instead — see below.

**TypeScript's arrow-function consts** (`const Foo = (...) => {...}`, the
dominant React-component style in `rhdh-plugins`) need an `extra_filter`
(`_is_arrow_function_const`) beyond the kind filter: the bare pattern
`const \NAME = \BODY` over-matches every `const` (data literals,
destructuring assignments where `NAME` captures literal `{ t }`/`[a, b]`
text instead of a real identifier). The filter requires `NAME` to be a real
identifier *and* `BODY` to start like a (possibly `async`) arrow function.

### Grammar resolution: `.ts` vs `.tsx`

`tree-sitter-typescript` ships two distinct grammars — `typescript` (no JSX
support) and `tsx` (JSX-aware) — despite one shared `"typescript"` config key
in `_DEF_SPECS`/`_CALL_PATTERNS`. `_grammar_for_path()` resolves the real
per-file grammar via CocoIndex's own `detect_code_language()` rather than
hand-rolling a second extension check. Feeding `.tsx` content to the plain
`typescript` grammar doesn't error — tree-sitter's error recovery quietly
produces a parse-error node around the JSX and keeps going — so getting this
wrong is silent, unbounded data loss from that point in the file onward, not
a visible failure.

## 2. Caller attribution: containment, not nearest-preceding-def

A call site needs to know which function's body it's inside, to become an
edge's `caller`. `match_code()` only ever reports the span of a definition's
*header* (through the closing `:`/opening `{`) — there is no pattern that
captures "the whole function, however long its body is" for every language.

Two strategies, chosen per language by whether its `DefSpec` has a `\BODY`
capture:

- **Exact body span** (TypeScript, Rust, Go): the `\BODY` capture *is* the
  exact body extent — tree-sitter treats a brace-delimited block (or an
  arrow function's entire node, body included) as one child, so
  `body_captures[0].end.line` is exact, no approximation needed.
- **Indentation-approximated body span** (Python, whose body isn't a single
  bracketed node): `_approximate_body_end_line()` scans forward from the end
  of the def's header until a non-blank line at indentation ≤ the def's own
  indentation appears. A heuristic, not a parse — assumes reasonably
  consistent indentation (safe for real Python source, not guaranteed for
  hostile/malformed input).

Either way, `_resolve_caller()` picks the **innermost** (smallest body span)
definition whose body contains the call's line — containment, not "nearest
preceding def by start line." The naive nearest-preceding version gets
nested functions wrong whenever a call to something else happens *after* a
nested def but still inside the *outer* function's body (very common: define
a small local closure, then call it later in the same enclosing function).

## 3. Call resolution: name-index + two precision filters + "one or nothing"

Resolution is **repo-wide within one call** and **trailing-segment-based**: a
call's bare or dotted/`::`-qualified name has its trailing segment extracted
(`bare_callee = callee_name.replace("::", ".").rsplit(".", 1)[-1]` — the
`::`→`.` normalization exists specifically for Rust's associated-function
syntax, `Foo::new(..)`) and looked up in a `name_index: dict[name, [qualified_names]]`
built from every definition found across the whole repo.

Following Graphify-Labs/graphify's precision-over-recall policy: **an edge
is only added when filtering narrows the candidate list to exactly one** —
never an arbitrary pick among several. Two filters narrow that list, applied
in order:

1. **Same-file preference**: if any candidate lives in the call site's own
   file, only same-file candidates are considered at all (the common
   "private helper" case — without this, a same-file call to a common helper
   name fans out to every same-named function repo-wide instead of the real
   same-file target).
2. **Signature-compatibility filtering** (`_signature_compatible`): a
   candidate is dropped if the call passes a keyword argument the
   candidate's parameter list doesn't accept (and the candidate has no
   `**kwargs` catch-all). Deliberately keyword-only, not positional-count —
   defaults, `*args`, and bound `self` make "how many positional args does
   this accept" too fuzzy to be reliable, whereas a keyword name is
   unambiguous when present. If this heuristic filter eliminates *every*
   candidate the name-index/same-file match legitimately found, resolution
   falls back to the unfiltered pool rather than trusting a heuristic parser
   over a real match.

The keyword-name signals themselves come from a lightweight bracket/quote-aware
splitter (`_split_top_level`, `_parse_def_param_names`,
`_parse_call_keyword_args`) — good enough to pull parameter/keyword-argument
*names* out of `match_code`'s raw captured text, not a real language parser.

**Outcomes, all first-class, none silent:**

| Outcome | What happens |
|---|---|
| Exactly 1 candidate survives filtering | Edge added |
| 0 candidates in `name_index` at all | Dropped, counted in `graph.graph["unresolved_calls"]` |
| 2+ candidates survive filtering | Dropped from the graph, but recorded in `graph.graph["ambiguous_calls"]` (caller, callee_name, display_path, line, candidates) — surfaced via `blast_radius`'s formatted output, never silently guessed |

## 4. Multi-repo aggregation

Some orgs' MCP server searches several independently-checked-out repos in
one call (`praxis.py`'s 7 Rust repos, `dcm.py`'s 8 Go repos, kubernaut's 2).
`build_multi_repo_call_graph()` takes the same `(repo_tag, root, included,
excluded)` tuple shape those orgs' existing `_PATTERN_SEARCH_ROOTS` already
use, walks and resolves **each repo independently** (own `build_call_graph`
call, own `repo_tag`-prefixed `display_path` so two repos sharing a relative
path stay disambiguated), then merges the resulting graphs by node/edge
union.

Resolution is deliberately **not** global across the merge — a call in one
repo can never resolve to a same-named function in a different repo, since
each repo's `name_index` is built and discarded before the next repo starts.
None of these orgs vendor shared library code across repo boundaries that
way, so this isn't a recall loss versus one combined walk, and it prevents
one repo's private helper names from silently leaking into another's
resolution (the multi-repo analogue of same-file preference within one
repo).

## 5. Clustering: Leiden over the undirected graph

`compute_clusters()` runs Leiden community detection (`igraph` +
`leidenalg`, `ModularityVertexPartition`) over the call graph **treated as
undirected** — call direction doesn't change which functions cluster
together architecturally. Returns `qualified_name -> cluster_id`. Clustering
quality depends entirely on the underlying graph's structure: a small,
centralized codebase may legitimately produce one dominant cluster or many
singletons — that reflects the codebase, not a broken clustering step.

## 6. Caching: Postgres, fingerprint-invalidated, no TTL (kubernaut only)

Every org except kubernaut rebuilds the graph fresh on every query — no
persisted index, no invalidation logic to get wrong, always-fresh results,
and every measured build stayed comfortably interactive (under ~33s even for
dcm's 8-repo Go build). kubernaut's own single repo (1,000+ Go files) took
~55s, too slow to pay per call, so it goes through
`build_multi_repo_call_graph_with_stats_cached()` instead.

**Why Postgres, not a dedicated cache service (Valkey/Redis):** Postgres
(`psycopg2`/`COCOINDEX_PG_URL`) is already a hard dependency of every
`*_search.py` module and already running for embeddings storage — reusing it
for one small `BYTEA` table adds zero new components to install, run, or
monitor. A dedicated TTL-native cache (Valkey) was tried first and reverted
before landing; see
[`CALL_GRAPH_CLUSTERING.md`'s Phase 5](CALL_GRAPH_CLUSTERING.md#phase-5-kubernaut-go----the-one-repo-that-broke-the-rebuild-every-call-budget-fixed-with-a-postgres-cache-instead-of-a-new-component)
for the full rationale.

**Explicitly no TTL.** A cache row (`cocoindex.call_graph_cache`: `cache_key`,
`fingerprint`, `graph_data BYTEA`, `updated_at`) is valid only if its stored
`fingerprint` matches a fresh one computed on every call —
`compute_fingerprint()` is a `stat()`-only walk (file count + max mtime
across the exact files the real build would walk), not a parse, measured at
well under a second even across ~1,000 files. A quiet tree never rebuilds
regardless of elapsed time; an edit is caught on the very next call rather
than waiting for an arbitrary expiry.

**Branch-awareness** (kubernaut only, since it's the only multi-branch
org): the cache key folds in the *resolved* release line (via the existing
`_resolve_release_line()`), not the raw `branch` argument passed in — so an
explicit `branch="v1.5"` and an auto-detected v1.5 checkout share one cache
entry, while main and v1.5 always land in distinct entries even when both
reach the cache via auto-detection.

**Failure mode:** any Postgres error (unreachable, corrupted pickle row, a
write failure after a real rebuild) is caught and logged, falling back to an
uncached rebuild — the same behavior every other org already has by default,
so a database hiccup degrades to "slower," never "broken."

## 7. Query layer

Three query functions, all resolving a user-given identifier first via
`resolve_node()` — which accepts either a full qualified name
(`"path/to/file.py::function_name"`) or a bare function name (resolves only
if unambiguous across the whole graph; otherwise returns the ambiguous
candidate list rather than guessing):

- `query_blast_radius(graph, function, depth)` — BFS over predecessors, one
  frontier per depth level.
- `query_shortest_path(graph, source, target)` — `networkx.shortest_path`.
- `query_get_cluster(graph, function)` — looks up `function`'s Leiden
  cluster, returns all members.

Each has a matching `format_*_result()` that renders the result (or a
lookup-error with candidates) as the plain-text an MCP tool returns.

## Known accuracy ceiling

This is **name-based resolution with no type information** — the same
ceiling every layer above (same-file preference, signature-compatibility
filtering, ambiguous-edge reporting) exists to narrow, not eliminate. A
function name duplicated across files with an *identical, compatible*
signature is a genuine, irreducible false-positive risk with this approach.
Measured against Serena/gopls ground truth: 100% recall, ~58% precision
before the signature-compatibility fix, with the false-positive rate
concentrated almost entirely in exactly this scenario — see
[`CALL_GRAPH_CLUSTERING.md`'s Serena/gopls cross-check](CALL_GRAPH_CLUSTERING.md#serenagopls-cross-check-measured-precisionrecall-not-just-the-unresolved-ratio)
and [Precision fix](CALL_GRAPH_CLUSTERING.md#precision-fix-signature-compatibility-filtering--graphify-style-ambiguous-edge-reporting)
sections for the exact numbers.

## Extending to a new language

1. Add a `DefSpec` tuple to `_DEF_SPECS[language]` — start with the simplest
   shape, add generic/return-type twins only if the language's grammar
   actually needs the annotation-arity split (verify empirically: preflight
   a return-typed/generic definition against your first pattern *before*
   assuming a plain pattern covers it — see the Phase 3/4 findings linked
   above for exactly this failure mode).
2. Add `_CALL_PATTERNS[language]` and `_CALL_KINDS[language]` for that
   language's call-expression shape.
3. If the language uses a non-`.`-based qualifier for path-qualified/
   associated-function calls (like Rust's `::`), extend the normalization in
   `build_call_graph`'s `bare_callee` computation.
4. Run the new language's patterns against real code (not just synthetic
   fixtures) before trusting them — every language added so far surfaced at
   least one real bug this way (see `CALL_GRAPH_CLUSTERING.md`'s per-phase
   entries).
5. Cross-check precision/recall against a real LSP (Serena/gopls) on a
   handful of real functions before declaring it done.
