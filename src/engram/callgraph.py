#!/usr/bin/env python3
"""Call-graph extraction + Leiden clustering spike (docs/CALL_GRAPH_CLUSTERING.md,
issue #43). Backs the `engram_call_graph_*` MCP tools.

Extraction is built entirely on CocoIndex's existing `match_code()` (see
docs/findings/2026-08.md, 2026-08-24 entry) -- no new tree-sitter dependency.
The same bare `\\NAME(\\(A*\\))` pattern matches both function definitions and
call expressions; `CodeMatch.kind` ("function_definition" vs. "call")
distinguishes them.

**Known limitation, load-bearing for this whole module**: `match_code()` only
reports the span of a def's *header* (through the closing `:`), never its
body -- there is no pattern that captures "the whole function, however long
its body is" (an omitted body means "don't care", not "capture it"). So a
call site's enclosing function can't be found via a real body-span
containment check. This module approximates one instead, using indentation:
scan forward from the end of a def's header until a non-blank line at
indentation <= the def's own indentation appears -- that's the approximate
end of its body. This is a heuristic, not a parse: it assumes reasonably
consistent indentation (a safe assumption for real Python source, not
guaranteed for hostile/malformed input) and can't see body ends CodePattern
itself can't see (e.g. multi-statement one-liners after a colon).

This approximation is still a meaningful improvement over the simpler
"nearest preceding def by start line" heuristic originally sketched for this
spike: that naive version gets nested functions wrong whenever a call to
something else happens *after* a nested def but still inside the *outer*
function's body (very common -- e.g. define a small local closure, then call
it later in the same enclosing function). See
`tests/test_callgraph.py::TestExtractCallSites` for the specific cases this
was validated against, including two more found while planning this spike:
a call inside a decorator's own argument list (resolves to no caller --
correct, since it isn't inside any function's body), and a module-level call
sitting between two function defs (same).

Name resolution is repo-wide: a call's bare/dotted name's *trailing* segment
is matched against every known qualified function name across the whole
graph, narrowed by two filters (see Serena cross-check findings,
docs/CALL_GRAPH_CLUSTERING.md -- name-alone resolution measured ~58%
precision, almost entirely from one duplicated-name function):

1. **Same-file preference**: if any candidate lives in the call site's own
 file, only same-file candidates are considered (the common "private
 helper" case).
2. **Signature-compatibility filtering**: a candidate is dropped if the
 call passes a keyword argument the candidate's parameter list doesn't
 accept (and the candidate has no `**kwargs` catch-all). This is the same
 signal that resolved the Serena cross-check's dominant false-positive
 case (a `repo=` keyword argument only some same-named candidates accept).

Following Graphify-Labs/graphify's precision-over-recall policy: an edge is
only added when filtering narrows the candidates to *exactly one* — never an
arbitrary pick among several. Calls matching zero known defs are dropped but
counted (`graph.graph["unresolved_calls"]`); calls where filtering still
leaves 2+ viable candidates are dropped from the graph too, but never
silently -- they're recorded in `graph.graph["ambiguous_calls"]` (list of
dicts: caller, callee_name, display_path, line, candidates) so callers of
this module can surface them rather than have them vanish. Both counters are
first-class signals of how much of a repo the extraction actually covers.

The signature/keyword parsing (`_split_top_level`, `_parse_def_param_names`,
`_parse_call_keyword_args`) is a lightweight bracket/quote-aware splitter,
not a real Python parser -- good enough to pull parameter/keyword-argument
*names* out of `match_code`'s raw captured argument-list text, not intended
to handle every possible Python expression shape correctly.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

import networkx as nx

# Per-language (def_pattern) config. Python-only for this spike (engram's own
# codebase); adding go/rust later is a matter of adding rows here and to
# _CALL_PATTERN if a language ever needs a different call shape (none of the
# languages surveyed for docs/CALL_GRAPH_CLUSTERING.md do -- `\NAME(\(A*\))`
# is a call expression in all three).
#
# Deliberately has NO trailing `:` -- discovered mid-spike (verified against
# this repo's own real, heavily-typed code) that `def \NAME(\(A*\)):` fails
# to match any function with a return-type annotation (`def f(x: int) -> int:`
# doesn't match `...):` since the annotation sits between the parameter
# list's closing paren and the colon). Dropping the trailing colon still
# matches only `function_definition`-kind nodes (never a bare call, which
# `\NAME(\(A*\))` alone -- see _CALL_PATTERN -- separately reports as kind
# "call"), and `CodeMatch.chunks[0].end.line` still lands on the correct
# physical header line either way (through the closing paren, which for any
# non-pathological formatting is the same line the colon is on).
_DEF_PATTERNS = {
    "python": r"def \NAME(\(A*\))",
}
_CALL_PATTERN = r"\NAME(\(A*\))"
_DEF_KIND = "function_definition"
_CALL_KIND = "call"


@dataclasses.dataclass(frozen=True)
class FunctionDef:
    qualified_name: str
    display_path: str
    name: str
    start_line: int
    body_end_line: int
    """Approximate last line still considered part of this def's body (see
    module docstring). Inclusive."""
    param_keywords: frozenset[str] = frozenset()
    """Names of parameters a caller could pass by keyword (i.e. everything
    except bare `*`/`/` separators and the `*args`/`**kwargs` names
    themselves). Used for signature-compatibility filtering."""
    accepts_arbitrary_keywords: bool = False
    """True if the signature has a `**kwargs`-shaped parameter, making it
    compatible with any keyword argument a caller passes."""


@dataclasses.dataclass(frozen=True)
class CallEdge:
    caller: str | None
    """Qualified name of the resolved enclosing def, or None if the call
    site isn't inside any known def's approximated body span (module-level
    code, decorator arguments, class-body-level code, etc.)."""
    callee_name: str
    display_path: str
    line: int
    keyword_args: frozenset[str] = frozenset()
    """Names of keyword arguments (`name=value` shape) passed at this call's
    top level. A `**forwarded`-style argument contributes nothing here --
    its contents can't be known statically."""


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _approximate_body_end_line(lines: list[str], header_end_line: int, def_indent: int) -> int:
    """1-indexed, inclusive. `header_end_line` is the last line of the def's
    header (i.e. the line with the closing `:` -- may be several lines below
    the `def` keyword for a multi-line signature); body scanning starts
    right after it, not right after the `def` line itself."""
    for i in range(header_end_line, len(lines)):  # lines[i] is 1-indexed line i+1
        line = lines[i]
        if not line.strip():
            continue
        if _indent_of(line) <= def_indent:
            return i  # last 0-indexed body line is i-1 -> 1-indexed i
    return len(lines)


_KEYWORD_ARG_RE = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)")
"""Matches a leading `name=` at the start of a call-argument chunk, but not
`name==value` (equality) or `name<=`/`name>=`/`name!=` -- the lookahead
rejects a second `=` immediately following, and the character class before
`=` only matches identifier characters, so `x >= y` (whose first char before
`=` is `>`) never matches at position 0 either."""


def _split_top_level(text: str) -> list[str]:
    """Split `text` on commas that aren't nested inside (), [], {}, or a
    quoted string. Good enough for pulling apart a parameter list or a call's
    argument list one item at a time; not a full Python parser (e.g. doesn't
    handle triple-quoted strings or f-string braces specially)."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote:
            current.append(ch)
            if ch == quote and text[i - 1] != "\\":
                quote = None
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _parse_def_param_names(param_text: str) -> tuple[frozenset[str], bool]:
    """Returns (param_keywords, accepts_arbitrary_keywords) from a def's raw
    captured parameter-list text (see `FunctionDef` field docs)."""
    keywords: set[str] = set()
    accepts_arbitrary_keywords = False
    for part in _split_top_level(param_text):
        if part in ("*", "/"):
            continue  # keyword-only/positional-only markers, not real params
        if part.startswith("**"):
            accepts_arbitrary_keywords = True
            continue
        if part.startswith("*"):
            continue  # *args -- can't be passed by keyword under its own name
        name = part.split(":", 1)[0].split("=", 1)[0].strip()
        if name.isidentifier():
            keywords.add(name)
    return frozenset(keywords), accepts_arbitrary_keywords


def _parse_call_keyword_args(arg_text: str) -> frozenset[str]:
    """Returns the set of keyword-argument names (`name=value` shape) at a
    call's top level, from its raw captured argument-list text. A
    `**forwarded` argument is skipped -- its contents aren't statically
    knowable, so it contributes no specific keyword names (see
    `_signature_compatible`, which treats missing information as "don't
    filter on this")."""
    keywords: set[str] = set()
    for part in _split_top_level(arg_text):
        if part.startswith("**"):
            continue
        m = _KEYWORD_ARG_RE.match(part)
        if m:
            keywords.add(m.group(1))
    return frozenset(keywords)


def extract_definitions(source: str, language: str, display_path: str) -> list[FunctionDef]:
    """Extract every function/method definition in `source`, sorted by
    start line. `display_path` is purely a label (used to build
    `qualified_name` and populate `display_path`/`file` fields) -- this
    function does no file I/O itself, so it's usable directly against a
    real file's content or a synthetic fixture string alike."""
    from cocoindex.ops.code import match_code

    pattern = _DEF_PATTERNS.get(language)
    if pattern is None:
        return []

    lines = source.splitlines()
    matches = match_code(pattern, source, language=language)
    defs: list[FunctionDef] = []
    for m in matches:
        if m.kind != _DEF_KIND:
            continue
        name_captures = m.captures.get("NAME")
        if not name_captures:
            continue
        name = name_captures[0].text
        chunk = m.chunks[0]
        start_line = chunk.start.line
        def_indent = _indent_of(lines[start_line - 1])
        body_end_line = _approximate_body_end_line(lines, chunk.end.line, def_indent)
        param_text = m.captures["A"][0].text if m.captures.get("A") else ""
        param_keywords, accepts_arbitrary_keywords = _parse_def_param_names(param_text)
        defs.append(
            FunctionDef(
                qualified_name=f"{display_path}::{name}",
                display_path=display_path,
                name=name,
                start_line=start_line,
                body_end_line=body_end_line,
                param_keywords=param_keywords,
                accepts_arbitrary_keywords=accepts_arbitrary_keywords,
            )
        )
    return sorted(defs, key=lambda d: d.start_line)


def _resolve_caller(defs_in_file: list[FunctionDef], line: int) -> str | None:
    """The innermost (smallest body span) def whose approximated body
    contains `line`, or None if no def's body contains it -- see module
    docstring for why containment (not "nearest preceding def by start
    line") is required for correct nested-function attribution."""
    containing = [d for d in defs_in_file if d.start_line <= line <= d.body_end_line]
    if not containing:
        return None
    return min(containing, key=lambda d: d.body_end_line - d.start_line).qualified_name


def extract_call_sites(
    source: str, language: str, display_path: str, defs_in_file: list[FunctionDef]
) -> list[CallEdge]:
    """Extract every call expression in `source` (kind == "call" only --
    the same bare pattern also matches function_definition headers and, for
    a call nested inside another call's arguments, a spurious
    "argument_list"-kind duplicate; both are dropped here)."""
    from cocoindex.ops.code import match_code

    if language not in _DEF_PATTERNS:
        return []

    matches = match_code(_CALL_PATTERN, source, language=language)
    edges: list[CallEdge] = []
    for m in matches:
        if m.kind != _CALL_KIND:
            continue
        name_captures = m.captures.get("NAME")
        if not name_captures:
            continue
        callee_name = name_captures[0].text
        line = m.chunks[0].start.line
        arg_text = m.captures["A"][0].text if m.captures.get("A") else ""
        edges.append(
            CallEdge(
                caller=_resolve_caller(defs_in_file, line),
                callee_name=callee_name,
                display_path=display_path,
                line=line,
                keyword_args=_parse_call_keyword_args(arg_text),
            )
        )
    return edges


def _signature_compatible(defn: FunctionDef, call: CallEdge) -> bool:
    """A candidate target is compatible with a call if it accepts every
    keyword argument the call passes (or has `**kwargs` and so accepts
    anything). Purely a *keyword* check -- arity/positional-count checking
    was deliberately left out: defaults, `*args`, and bound `self` all make
    "how many positional args does this accept" too fuzzy to be a reliable
    filter, whereas a keyword name is unambiguous when present."""
    if defn.accepts_arbitrary_keywords:
        return True
    return call.keyword_args <= defn.param_keywords


def build_call_graph(
    root: pathlib.Path,
    included: list[str],
    excluded: list[str],
    language: str,
) -> nx.DiGraph:
    """Walk `root` (reusing the same `chunking.find_code_files()` every
    `*_code_pattern_search` tool already uses, so this never drifts from
    what's actually indexed), extract every def + call site, and resolve
    calls into a directed graph: node = qualified function name, edge =
    caller calls callee.

    Callee resolution is repo-wide and trailing-segment-based: a dotted
    call name's last segment is matched against every known qualified name,
    then narrowed by same-file preference and signature-compatibility
    filtering (see module docstring). An edge is only added when exactly one
    candidate survives -- Graphify's precision-over-recall policy, not an
    arbitrary pick among several. Calls matching zero known defs are dropped
    but counted in `graph.graph["unresolved_calls"]`; calls where 2+
    candidates remain after filtering are dropped from the graph but
    recorded in `graph.graph["ambiguous_calls"]` instead of vanishing.
    """
    from engram import chunking

    graph = nx.DiGraph()
    graph.graph["unresolved_calls"] = 0
    graph.graph["total_calls"] = 0
    graph.graph["ambiguous_calls"] = []

    all_defs: list[FunctionDef] = []
    calls_by_file: list[list[CallEdge]] = []

    for path in chunking.find_code_files(root, included, excluded):
        display_path = str(path.relative_to(root))
        source = path.read_text(encoding="utf-8", errors="ignore")
        defs_in_file = extract_definitions(source, language, display_path)
        all_defs.extend(defs_in_file)
        calls_by_file.append(extract_call_sites(source, language, display_path, defs_in_file))

    for d in all_defs:
        graph.add_node(d.qualified_name)

    defs_by_qualified_name = {d.qualified_name: d for d in all_defs}
    name_index: dict[str, list[str]] = {}
    for d in all_defs:
        name_index.setdefault(d.name, []).append(d.qualified_name)

    for calls in calls_by_file:
        for call in calls:
            if call.caller is None:
                continue
            graph.graph["total_calls"] += 1
            bare_callee = call.callee_name.rsplit(".", 1)[-1]
            targets = name_index.get(bare_callee)
            if not targets:
                graph.graph["unresolved_calls"] += 1
                continue
            # Prefer a same-file match (the common "private helper" case,
            # e.g. dcm.py's main() calling dcm.py's own _run_cli_pattern_query)
            # over fanning out to every same-named function repo-wide.
            # Discovered mid-spike verifying against this repo's own several
            # near-identical *_code_pattern_search modules: without this, a
            # same-file call got a spurious edge to all 6 modules' identically
            # named private helper, and shortest_path/blast_radius results
            # picked an arbitrary one of those instead of the real same-file
            # call. Only fall back to the repo-wide (possibly multi-target)
            # match when nothing in the same file defines that name.
            same_file_prefix = f"{call.display_path}::"
            same_file_targets = [t for t in targets if t.startswith(same_file_prefix)]
            pool = same_file_targets or targets

            compatible = [t for t in pool if _signature_compatible(defs_by_qualified_name[t], call)]
            if not compatible:
                # Our own keyword parsing is a heuristic, not a real parser
                # (see _parse_def_param_names/_parse_call_keyword_args) --
                # if it eliminates every candidate the same_file/name_index
                # match legitimately found, don't trust it over silently
                # discarding a real match; fall back to the unfiltered pool.
                compatible = pool

            if len(compatible) == 1:
                graph.add_edge(call.caller, compatible[0])
            else:
                graph.graph["ambiguous_calls"].append(
                    {
                        "caller": call.caller,
                        "callee_name": call.callee_name,
                        "display_path": call.display_path,
                        "line": call.line,
                        "candidates": sorted(compatible),
                    }
                )

    return graph


def compute_clusters(graph: nx.DiGraph) -> dict[str, int]:
    """Leiden community detection over `graph`, treated as undirected (Leiden
    is conventionally run on undirected graphs; call direction doesn't change
    which functions cluster together architecturally). Returns
    qualified_name -> cluster id. An empty graph returns an empty dict."""
    import igraph as ig
    import leidenalg

    if graph.number_of_nodes() == 0:
        return {}

    ig_graph = ig.Graph.from_networkx(graph.to_undirected())
    partition = leidenalg.find_partition(ig_graph, leidenalg.ModularityVertexPartition)
    return {ig_graph.vs[i]["_nx_name"]: partition.membership[i] for i in range(len(ig_graph.vs))}
