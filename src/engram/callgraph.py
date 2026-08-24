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

Name resolution is intentionally simple and repo-wide: a call's bare/dotted
name's *trailing* segment is matched against every known qualified function
name across the whole graph. Common method names shared by unrelated
functions/classes will produce false-positive edges; calls to names CocoIndex
never saw a definition for (external libraries, builtins, dynamic dispatch)
are dropped but counted (`graph.graph["unresolved_calls"]`) as a first-class
signal of how much of a bank the extraction actually covers -- not swallowed
silently.
"""
from __future__ import annotations

import dataclasses
import pathlib

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


@dataclasses.dataclass(frozen=True)
class CallEdge:
    caller: str | None
    """Qualified name of the resolved enclosing def, or None if the call
    site isn't inside any known def's approximated body span (module-level
    code, decorator arguments, class-body-level code, etc.)."""
    callee_name: str
    display_path: str
    line: int


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
        defs.append(
            FunctionDef(
                qualified_name=f"{display_path}::{name}",
                display_path=display_path,
                name=name,
                start_line=start_line,
                body_end_line=body_end_line,
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
        edges.append(
            CallEdge(
                caller=_resolve_caller(defs_in_file, line),
                callee_name=callee_name,
                display_path=display_path,
                line=line,
            )
        )
    return edges


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

    Callee resolution is repo-wide and trailing-segment-based: a dotted call
    name's last segment is matched against every known qualified name.
    Common names shared across files/classes get an edge to *every* match
    (conservative -- documented over-connection risk, see module docstring)
    rather than an arbitrary pick. Calls matching zero known defs are
    dropped but counted in `graph.graph["unresolved_calls"]`.
    """
    from engram import chunking

    graph = nx.DiGraph()
    graph.graph["unresolved_calls"] = 0
    graph.graph["total_calls"] = 0

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
            for target in same_file_targets or targets:
                graph.add_edge(call.caller, target)

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
