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

**Multi-org query/format layer** (`build_call_graph_with_stats`,
`resolve_node`, `query_*`, `format_*` below): originally these lived as
engram-only private helpers in `engram/search/engram.py`. They contain
nothing engram-specific -- only *which* root/language to build from differs
per org -- so as of the multi-org rollout (docs/CALL_GRAPH_CLUSTERING.md)
they live here instead, and every org's `*_call_graph_*` MCP tools
(starting with engram's own) are thin wrappers around this shared layer.
"""
from __future__ import annotations

import dataclasses
import logging
import pathlib
import re
import time
from typing import Any, Callable

import networkx as nx

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class DefSpec:
    """One pattern+kind(s) shape that counts as a function/method definition.

    A language may need more than one of these -- e.g. TypeScript needs a
    `function`-keyword pattern for declarations, a bare pattern filtered to
    `method_definition` for class methods, and a third pattern for
    arrow-function consts (`const Foo = (...) => {...}`), which aren't
    reachable through either of the other two shapes.

    `pattern` ending in `\\BODY` captures the definition's *whole* body as one
    node (tree-sitter treats a brace-delimited block, or -- for an arrow
    function -- the entire `arrow_function` node including its body, as a
    single child), giving an exact `body_end_line` instead of the
    indentation-approximated one `_approximate_body_end_line` falls back to
    for a pattern with no `\\BODY` capture (Python's, whose body isn't a
    single bracketed node `match_code` can capture as one unit).
    """
    pattern: str
    kinds: frozenset[str]
    extra_filter: Callable[[str, str], bool] | None = None
    """Optional (name_text, body_text) -> bool predicate, checked after the
    kind filter. Only TypeScript's arrow-const spec uses this: `const \\NAME
    = \\BODY` alone over-matches *every* const (data literals, destructuring
    assignments where NAME captures literal `{...}`/`[...]` text instead of
    an identifier, etc.) -- see `_is_arrow_function_const`."""


_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_ARROW_BODY_RE = re.compile(r"^(async\s+)?(\(|[A-Za-z_$][A-Za-z0-9_$]*\s*=>)")


def _is_arrow_function_const(name_text: str, body_text: str) -> bool:
    """Filters `const \\NAME = \\BODY` matches down to real arrow-function
    components. Two checks, both required (verified against the real
    rhdh-plugins/workspaces/boost corpus, docs/CALL_GRAPH_CLUSTERING.md):

    1. `name_text` must be a plain identifier -- a destructuring assignment
       (`const { t } = useTranslation()`, `const [a, b] = useState()`) makes
       tree-sitter's NAME capture the literal `{ t }`/`[a, b]` text instead of
       a real name, which this rejects.
    2. `body_text` must *start* like a (possibly `async`) arrow function --
       `(` for a parameter list, or a single bare identifier immediately
       followed by `=>` for a one-arg arrow with no parens (`x => x + 1`).
       Rejects plain data consts (`const N = 60`), object/regex/string
       literals, and `const X = someFactory({...})`-shaped calls (a common
       false-positive source: a `=>` appears deep inside the call's own
       arguments, but the const's own value is a call, not a function).
    """
    return bool(_IDENT_RE.match(name_text)) and bool(_ARROW_BODY_RE.match(body_text.strip()))


def _looks_like_real_block_body(text: str) -> bool:
    """Guards against a `match_code` positional-matching quirk found while
    preflighting Rust/TS return-typed functions (2026-08-24, Phase 3): a
    brace-body `DefSpec` pattern written for one return-type "arity" (e.g.
    no explicit return type) doesn't fail to match a definition with a
    *different* arity (e.g. `fn f() -> T { .. }` / `function f(): T { .. }`)
    -- it still matches, but `\\BODY` silently binds to the return-type
    annotation's own text (the literal `"->"` token for Rust; `": T"` for
    TypeScript) instead of the real block, because the pattern has no field
    in that position to align the annotation against. This is invisible in
    isolation (still "a match"), and dangerous rather than merely
    incomplete: it produces a `FunctionDef` with a wrong, tiny
    `body_end_line` (often equal to its own `start_line`) instead of no
    entry at all, silently breaking caller attribution for every call
    inside that function's real body.

    Every real block body, in every brace-delimited language this module
    supports, starts with `{` once stripped -- no return-type annotation
    text ever does -- so this one check reliably tells them apart. Only
    applies when a spec has no `extra_filter` of its own (arrow-function
    consts have a legitimately different, non-`{`-prefixed body shape,
    already validated by `_is_arrow_function_const`)."""
    return text.strip().startswith("{")


# Per-language config. `language` here is the *config key* an org passes to
# build_call_graph() -- not necessarily the literal grammar name passed to
# match_code(): see `_grammar_for_path`, which resolves the real per-file
# grammar (needed because TypeScript's `.ts` and `.tsx` files require two
# different tree-sitter grammars -- "typescript" has no JSX support at all,
# "tsx" does -- despite sharing one language config below; see
# docs/CALL_GRAPH_CLUSTERING.md, 2026-08-24 Phase 2 entry).
#
# Every brace-bodied shape below (TypeScript function decls/methods, Rust
# fns, Go funcs/methods) needs its DefSpecs split along up to TWO
# independent axes, each verified empirically to need its own literal
# pattern rather than one "flexible" pattern (see
# docs/CALL_GRAPH_CLUSTERING.md, 2026-08-24 Phase 2/3/4 entries;
# `_looks_like_real_block_body`'s docstring explains the failure mode):
#
# 1. generic/lifetime type params (`<T>`/`<'a>` for TS/Rust, `[T any]` for
#    Go) -- a pattern with the literal bracket pair in it only matches a def
#    that actually has them, and a plain pattern never matches one that
#    does. Go methods are the one exception: a method can't have its own
#    type parameters (only its receiver type can be generic, e.g.
#    `func (s *Server[T]) M(x T) T`), and that's already fully inside the
#    `\(R*\)` receiver capture -- so Go's `method_declaration` spec only
#    needs the return-type axis below, not this one.
# 2. an explicit return-type annotation (`-> T` for Rust, `: T` for
#    TypeScript, bare ` T`/` (a, b)` for Go -- no separator token at all) --
#    same story: a pattern missing the annotation doesn't just fail to
#    match a def that has one, it matches with `\BODY` bound to the
#    annotation's own text instead of the real block (a *wrong* result, not
#    a missing one) -- so every DefSpec without an explicit return-type
#    placeholder must be paired with an explicit-return-type twin, and
#    `_looks_like_real_block_body` (applied below in extract_definitions)
#    discards whichever twin's `\BODY` capture doesn't actually look like a
#    block, since a def can only ever really satisfy one of the two.
#
# That's 2x2 = 4 patterns per brace-bodied node kind for TypeScript
# (`function_declaration`, `method_definition`) and Rust (`function_item`,
# covering free fns, impl methods, and trait-impl methods alike --
# visibility/async modifiers and enclosing impl/trait blocks are separate
# AST nodes, not part of this match); Go needs 4 for `function_declaration`
# but only 2 (just the return-type axis) for `method_declaration` per the
# generics exception above. A bodyless interface method signature (Go's
# `method_elem`) or trait method signature (Rust's
# `function_signature_item`) is a different kind from its real,
# body-bearing counterpart in each language, naturally excluded by the kind
# filter without needing its own spec.
#
# Python's def pattern deliberately has NO trailing `:` -- discovered
# mid-spike (verified against this repo's own real, heavily-typed code) that
# `def \NAME(\(A*\)):` fails to match any function with a return-type
# annotation (`def f(x: int) -> int:` doesn't match `...):` since the
# annotation sits between the parameter list's closing paren and the colon).
# Dropping the trailing colon still matches only `function_definition`-kind
# nodes (never a bare call, which the call pattern below separately reports
# as kind "call"), and `CodeMatch.chunks[0].end.line` still lands on the
# correct physical header line either way (through the closing paren, which
# for any non-pathological formatting is the same line the colon is on). It
# has no `\BODY` capture -- Python's body isn't a single bracketed node --
# so its body end is approximated instead (see `_approximate_body_end_line`),
# and the generic/return-type-annotation split above doesn't apply to it at
# all (no `\BODY` capture means no wrong-body-binding failure mode to guard
# against).
_DEF_SPECS: dict[str, tuple[DefSpec, ...]] = {
    "python": (
        DefSpec(pattern=r"def \NAME(\(A*\))", kinds=frozenset({"function_definition"})),
    ),
    "typescript": (
        DefSpec(pattern=r"function \NAME(\(A*\)) \BODY", kinds=frozenset({"function_declaration"})),
        DefSpec(pattern=r"function \NAME<\(G*\)>(\(A*\)) \BODY", kinds=frozenset({"function_declaration"})),
        DefSpec(pattern=r"function \NAME(\(A*\)): \RET \BODY", kinds=frozenset({"function_declaration"})),
        DefSpec(pattern=r"function \NAME<\(G*\)>(\(A*\)): \RET \BODY", kinds=frozenset({"function_declaration"})),
        DefSpec(pattern=r"\NAME(\(A*\)) \BODY", kinds=frozenset({"method_definition"})),
        DefSpec(pattern=r"\NAME<\(G*\)>(\(A*\)) \BODY", kinds=frozenset({"method_definition"})),
        DefSpec(pattern=r"\NAME(\(A*\)): \RET \BODY", kinds=frozenset({"method_definition"})),
        DefSpec(pattern=r"\NAME<\(G*\)>(\(A*\)): \RET \BODY", kinds=frozenset({"method_definition"})),
        DefSpec(
            pattern=r"const \NAME = \BODY",
            kinds=frozenset({"lexical_declaration"}),
            extra_filter=_is_arrow_function_const,
        ),
    ),
    "rust": (
        DefSpec(pattern=r"fn \NAME(\(A*\)) \BODY", kinds=frozenset({"function_item"})),
        DefSpec(pattern=r"fn \NAME<\(G*\)>(\(A*\)) \BODY", kinds=frozenset({"function_item"})),
        DefSpec(pattern=r"fn \NAME(\(A*\)) -> \RET \BODY", kinds=frozenset({"function_item"})),
        DefSpec(pattern=r"fn \NAME<\(G*\)>(\(A*\)) -> \RET \BODY", kinds=frozenset({"function_item"})),
    ),
    "go": (
        DefSpec(pattern=r"func \NAME(\(A*\)) \BODY", kinds=frozenset({"function_declaration"})),
        DefSpec(pattern=r"func \NAME[\(G*\)](\(A*\)) \BODY", kinds=frozenset({"function_declaration"})),
        DefSpec(pattern=r"func \NAME(\(A*\)) \RET \BODY", kinds=frozenset({"function_declaration"})),
        DefSpec(pattern=r"func \NAME[\(G*\)](\(A*\)) \RET \BODY", kinds=frozenset({"function_declaration"})),
        DefSpec(pattern=r"func (\(R*\)) \NAME(\(A*\)) \BODY", kinds=frozenset({"method_declaration"})),
        DefSpec(pattern=r"func (\(R*\)) \NAME(\(A*\)) \RET \BODY", kinds=frozenset({"method_declaration"})),
    ),
}
_CALL_PATTERNS: dict[str, str] = {
    "python": r"\NAME(\(A*\))",
    "typescript": r"\NAME(\(A*\))",
    "rust": r"\NAME(\(A*\))",
    "go": r"\NAME(\(A*\))",
}
_CALL_KINDS: dict[str, frozenset[str]] = {
    "python": frozenset({"call"}),
    "typescript": frozenset({"call_expression"}),
    "rust": frozenset({"call_expression"}),
    "go": frozenset({"call_expression"}),
}


def _grammar_for_path(display_path: str, language: str) -> str:
    """The actual tree-sitter grammar to pass to match_code() for this file.

    Almost always just `language` itself -- but TypeScript's `.ts`/`.tsx`
    extensions need two different grammars despite one shared `"typescript"`
    config key above (`detect_code_language` already knows this distinction
    perfectly; reuse it instead of hand-rolling a second extension check).
    Feeding `.tsx` (JSX) content to the plain "typescript" grammar (no JSX
    support) doesn't error -- tree-sitter's error recovery quietly produces a
    parse-error node around the JSX and keeps going -- so this isn't a
    correctness edge case, it's silent, unbounded data loss from that point
    in the file onward. See docs/CALL_GRAPH_CLUSTERING.md, 2026-08-24 Phase 2
    entry, for how this was found (a single real ~150-line miss looked like a
    match_code bug until traced to this)."""
    from cocoindex.ops.text import detect_code_language

    return detect_code_language(filename=pathlib.Path(display_path).name) or language


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
    `qualified_name` and populate `display_path`/`file` fields, and to
    resolve the real per-file grammar -- see `_grammar_for_path`) -- this
    function does no file I/O itself, so it's usable directly against a
    real file's content or a synthetic fixture string alike.

    Runs every `DefSpec` registered for `language` (see `_DEF_SPECS`) and
    merges their results -- a language may need several distinct pattern
    shapes to cover every way it defines a function (TypeScript needs nine,
    Rust needs four -- see `_DEF_SPECS`'s module-level comment for why).
    Two universal guards apply across every spec (not just ones with their
    own `extra_filter`), both discovered via the same 2026-08-24 Phase 3
    investigation into wrong-shape pattern/definition mismatches:

    - `NAME` must be a real identifier -- a spec applied to a definition
      shaped for a *different* spec (e.g. the no-generic pattern against a
      generic def) can still "match", with `NAME` bound to some unrelated
      fragment of the signature (observed: literal `"<T>"` text) instead of
      the real name.
    - Absent a spec-specific `extra_filter`, a `BODY` capture must look like
      a real block (see `_looks_like_real_block_body`) -- the return-type-
      annotation-arity mismatch described in `_DEF_SPECS`'s comment.

    Both guards mean a def with the "wrong" spec applied to it is silently
    dropped rather than kept with garbage data -- exactly one of a def's
    matching specs (by construction, since the generic/return-type axes are
    mutually exclusive per real definition) should ever survive both."""
    from cocoindex.ops.code import match_code

    specs = _DEF_SPECS.get(language)
    if not specs:
        return []

    grammar = _grammar_for_path(display_path, language)
    lines = source.splitlines()
    defs: list[FunctionDef] = []
    for spec in specs:
        matches = match_code(spec.pattern, source, language=grammar)
        for m in matches:
            if m.kind not in spec.kinds:
                continue
            name_captures = m.captures.get("NAME")
            if not name_captures:
                continue
            name = name_captures[0].text
            if not _IDENT_RE.match(name):
                continue
            body_captures = m.captures.get("BODY")
            if spec.extra_filter is not None:
                body_text = body_captures[0].text if body_captures else ""
                if not spec.extra_filter(name, body_text):
                    continue
            elif body_captures is not None and not _looks_like_real_block_body(body_captures[0].text):
                continue
            chunk = m.chunks[0]
            start_line = chunk.start.line
            if body_captures is not None:
                # Real body extent (see DefSpec docstring) -- no approximation needed.
                body_end_line = body_captures[0].end.line
            else:
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
    """Extract every call expression in `source` (kind filtered to this
    language's `_CALL_KINDS` entry only -- the same bare pattern also
    matches definition headers and, for a call nested inside another call's
    arguments, a spurious "argument_list"-kind duplicate; both are dropped
    here)."""
    from cocoindex.ops.code import match_code

    call_pattern = _CALL_PATTERNS.get(language)
    call_kinds = _CALL_KINDS.get(language)
    if call_pattern is None or call_kinds is None:
        return []

    grammar = _grammar_for_path(display_path, language)
    matches = match_code(call_pattern, source, language=grammar)
    edges: list[CallEdge] = []
    for m in matches:
        if m.kind not in call_kinds:
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
    repo_tag: str = "",
) -> nx.DiGraph:
    """Walk `root` (reusing the same `chunking.find_code_files()` every
    `*_code_pattern_search` tool already uses, so this never drifts from
    what's actually indexed), extract every def + call site, and resolve
    calls into a directed graph: node = qualified function name, edge =
    caller calls callee.

    Callee resolution is repo-wide (within this one call) and
    trailing-segment-based: a dotted/`::`-qualified call name's last segment
    is matched against every known qualified name, then narrowed by
    same-file preference and signature-compatibility filtering (see module
    docstring). An edge is only added when exactly one candidate survives --
    Graphify's precision-over-recall policy, not an arbitrary pick among
    several. Calls matching zero known defs are dropped but counted in
    `graph.graph["unresolved_calls"]`; calls where 2+ candidates remain
    after filtering are dropped from the graph but recorded in
    `graph.graph["ambiguous_calls"]` instead of vanishing.

    `repo_tag`, if given, is prefixed onto every `display_path` as
    `f"{repo_tag}/{rel_path}"` -- for orgs whose MCP tool searches several
    independent repo checkouts in one call (`praxis.py`'s 7 Rust repos,
    `dcm.py`'s 8 Go repos), this keeps qualified names disambiguated by repo
    even if two repos happen to share a relative file path. See
    `build_multi_repo_call_graph`, the caller that actually supplies this.
    """
    from engram import chunking

    graph = nx.DiGraph()
    graph.graph["unresolved_calls"] = 0
    graph.graph["total_calls"] = 0
    graph.graph["ambiguous_calls"] = []

    all_defs: list[FunctionDef] = []
    calls_by_file: list[list[CallEdge]] = []

    for path in chunking.find_code_files(root, included, excluded):
        rel_path = str(path.relative_to(root))
        display_path = f"{repo_tag}/{rel_path}" if repo_tag else rel_path
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
            # Rust's associated-function/path-qualified calls (`Foo::new(..)`,
            # `std::mem::swap(..)`) use `::`, not `.`, as the segment
            # separator -- normalize both to get the trailing name either
            # way. `.` alone would leave `Foo::new` unsplit (no literal `.`
            # in it) and it would never resolve against name_index, which
            # only ever stores bare names.
            bare_callee = call.callee_name.replace("::", ".").rsplit(".", 1)[-1]
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


# ---------------------------------------------------------------------------
# Multi-org query/format layer (see module docstring)
# ---------------------------------------------------------------------------
#
# Live-rebuild-per-query, deliberately, for every org (scope confirmed for
# the rollout): every query below re-walks the target root fresh via
# build_call_graph_with_stats() rather than reading a persisted index.
# Whether any given org's repo needs persistence is a decision made later
# from the elapsed-time/node/edge numbers logged here, not decided up front
# -- see docs/CALL_GRAPH_CLUSTERING.md.

def build_call_graph_with_stats(
    root: pathlib.Path,
    included: list[str],
    excluded: list[str],
    language: str,
    logger: logging.Logger | None = None,
) -> nx.DiGraph:
    """build_call_graph() plus elapsed-time/size logging, shared by every
    org's `*_call_graph_*` tools. Pass the calling module's own `logger` so
    log lines are attributed to that org's MCP server, not to this module."""
    logger = logger or log
    start = time.monotonic()
    graph = build_call_graph(root, included=included, excluded=excluded, language=language)
    elapsed = time.monotonic() - start
    logger.info(
        "call graph built in %.2fs (%d nodes, %d edges, %d/%d calls unresolved, %d ambiguous)",
        elapsed, graph.number_of_nodes(), graph.number_of_edges(),
        graph.graph.get("unresolved_calls", 0), graph.graph.get("total_calls", 0),
        len(graph.graph.get("ambiguous_calls", [])),
    )
    return graph


def build_multi_repo_call_graph(
    roots: list[tuple[str, pathlib.Path, list[str], list[str]]],
    language: str,
) -> nx.DiGraph:
    """Same contract as `build_call_graph`, but for orgs whose MCP server
    searches several independently-checked-out repos in one call (`praxis.py`'s
    7 Rust repos, `dcm.py`'s 8 Go repos) -- `roots` is that org's
    `_PATTERN_SEARCH_ROOTS` list verbatim: `(repo_tag, root, included,
    excluded)` tuples.

    Each repo is walked and resolved independently (own `build_call_graph`
    call, own `repo_tag`-prefixed display paths) and the resulting graphs are
    merged by node/edge union; resolution is deliberately *not* global
    across the merge -- a call in one repo can never resolve to a
    same-named function in another repo, since each repo's `name_index` is
    built and discarded before the next repo starts. None of these orgs
    vendor shared library code across repo boundaries that way, so this
    isn't a recall loss versus a single combined walk, and it keeps one
    repo's private helper names from silently leaking into another's
    resolution the same way same-file preference already prevents that
    within a single repo (see `build_call_graph`'s docstring)."""
    graph = nx.DiGraph()
    graph.graph["unresolved_calls"] = 0
    graph.graph["total_calls"] = 0
    graph.graph["ambiguous_calls"] = []
    for repo_tag, root, included, excluded in roots:
        repo_graph = build_call_graph(root, included=included, excluded=excluded, language=language, repo_tag=repo_tag)
        graph.add_nodes_from(repo_graph.nodes)
        graph.add_edges_from(repo_graph.edges)
        graph.graph["unresolved_calls"] += repo_graph.graph.get("unresolved_calls", 0)
        graph.graph["total_calls"] += repo_graph.graph.get("total_calls", 0)
        graph.graph["ambiguous_calls"].extend(repo_graph.graph.get("ambiguous_calls", []))
    return graph


def build_multi_repo_call_graph_with_stats(
    roots: list[tuple[str, pathlib.Path, list[str], list[str]]],
    language: str,
    logger: logging.Logger | None = None,
) -> nx.DiGraph:
    """`build_multi_repo_call_graph()` plus elapsed-time/size logging --
    the multi-repo counterpart to `build_call_graph_with_stats`."""
    logger = logger or log
    start = time.monotonic()
    graph = build_multi_repo_call_graph(roots, language=language)
    elapsed = time.monotonic() - start
    logger.info(
        "call graph built in %.2fs across %d repos (%d nodes, %d edges, %d/%d calls unresolved, %d ambiguous)",
        elapsed, len(roots), graph.number_of_nodes(), graph.number_of_edges(),
        graph.graph.get("unresolved_calls", 0), graph.graph.get("total_calls", 0),
        len(graph.graph.get("ambiguous_calls", [])),
    )
    return graph


# ---------------------------------------------------------------------------
# Postgres-backed cache (kubernaut only, docs/CALL_GRAPH_CLUSTERING.md
# 2026-08-24 Phase 5)
#
# Every other org's full rebuild-per-call stayed under 33s, an accepted cost
# for always-fresh results with zero cache-invalidation logic to get wrong.
# kubernaut's ~55s single-repo build broke that budget badly enough to
# revisit it -- but the fix reuses Postgres (already a hard dependency for
# every org's search_code(), already running, already shared across
# processes) rather than adding a new service: a `psycopg2-binary` import
# every org already carries, one small BYTEA table, no new component to
# install/run/monitor.
#
# Explicitly NOT time-based (no TTL): a fixed expiry would force a rebuild
# of an *unchanged* tree on some arbitrary schedule -- wasted work on a
# quiet weekend, and still no guarantee of catching an edit made just
# before expiry anyway. Invalidation is instead keyed on a content
# fingerprint (file count + max mtime across the exact same included/
# excluded globs the build itself walks) checked on every call: a cache
# entry is used only if the fingerprint matches bit-for-bit, so an edit is
# always caught on the very next call, and a quiet tree never rebuilds at
# all, no matter how much wall-clock time passes. The fingerprint check
# itself is a stat()-only walk (no parsing), measured at well under a
# second even across kubernaut's ~1,000+ files -- negligible next to either
# a cache hit or a 25s+ rebuild.
#
# Any error talking to Postgres (unreachable, table missing and somehow
# uncreatable, corrupted row) is caught and logged, falling back to an
# uncached rebuild -- exactly the pre-Phase-5 behavior every other org
# already has -- rather than failing the tool call outright.
# ---------------------------------------------------------------------------

_CACHE_TABLE_READY = False


def _ensure_cache_table(conn) -> None:
    """Idempotent -- safe to call on every cache access, but only actually
    issues DDL once per process via the module-level ready flag."""
    global _CACHE_TABLE_READY
    if _CACHE_TABLE_READY:
        return
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS cocoindex")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cocoindex.call_graph_cache (
                cache_key TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                graph_data BYTEA NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()
    _CACHE_TABLE_READY = True


def compute_fingerprint(roots: list[tuple[str, pathlib.Path, list[str], list[str]]]) -> str:
    """Cheap staleness signal for `roots` (same `(repo_tag, root, included,
    excluded)` shape as `build_multi_repo_call_graph`): file count + max
    mtime across every file `chunking.find_code_files` would walk for the
    real build. A `stat()`-only pass, not a parse -- see this section's
    module-level comment for why this (not a TTL) is the cache's
    invalidation signal."""
    from engram import chunking

    max_mtime = 0.0
    count = 0
    for _repo_tag, root, included, excluded in roots:
        for path in chunking.find_code_files(root, included, excluded):
            count += 1
            mtime = path.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
    return f"{count}:{max_mtime:.6f}"


def build_multi_repo_call_graph_with_stats_cached(
    roots: list[tuple[str, pathlib.Path, list[str], list[str]]],
    language: str,
    cache_key: str,
    pg_url: str,
    logger: logging.Logger | None = None,
) -> nx.DiGraph:
    """`build_multi_repo_call_graph_with_stats()`, but checking a Postgres-
    backed cache (`cocoindex.call_graph_cache`) first. `cache_key` scopes
    the cache row (e.g. "kubernaut-go" to distinguish it from a future
    second cached org). See this section's module-level comment for the
    fingerprint-only (no TTL) invalidation policy and the fall-back-to-
    uncached-rebuild behavior on any Postgres error."""
    import pickle

    import psycopg2

    logger = logger or log
    fingerprint = compute_fingerprint(roots)

    try:
        conn = psycopg2.connect(pg_url)
        try:
            _ensure_cache_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fingerprint, graph_data FROM cocoindex.call_graph_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("call graph cache unavailable for %s (read) -- rebuilding uncached", cache_key, exc_info=True)
        return build_multi_repo_call_graph_with_stats(roots, language=language, logger=logger)

    if row is not None and row[0] == fingerprint:
        try:
            graph = pickle.loads(bytes(row[1]))
        except Exception:
            logger.warning("call graph cache row for %s is corrupt -- rebuilding uncached", cache_key, exc_info=True)
        else:
            logger.info(
                "call graph cache HIT for %s (fingerprint %s, %d nodes, %d edges, %d/%d calls unresolved, %d ambiguous)",
                cache_key, fingerprint, graph.number_of_nodes(), graph.number_of_edges(),
                graph.graph.get("unresolved_calls", 0), graph.graph.get("total_calls", 0),
                len(graph.graph.get("ambiguous_calls", [])),
            )
            return graph

    logger.info("call graph cache MISS for %s (fingerprint %s) -- rebuilding", cache_key, fingerprint)
    graph = build_multi_repo_call_graph_with_stats(roots, language=language, logger=logger)

    try:
        conn = psycopg2.connect(pg_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cocoindex.call_graph_cache (cache_key, fingerprint, graph_data, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (cache_key) DO UPDATE
                        SET fingerprint = EXCLUDED.fingerprint,
                            graph_data = EXCLUDED.graph_data,
                            updated_at = now()
                    """,
                    (cache_key, fingerprint, pickle.dumps(graph)),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("failed to persist call graph cache for %s -- next call will rebuild again", cache_key, exc_info=True)

    return graph


def resolve_node(graph: nx.DiGraph, identifier: str) -> tuple[str | None, list[str]]:
    """Resolve a user-given identifier to exactly one graph node.

    Accepts either a full qualified name ("search/engram.py::pattern_search_code")
    or a bare function name ("pattern_search_code") -- the latter resolves
    only if unambiguous. Returns (resolved_name, candidates); resolved_name
    is None if zero or more-than-one match was found, in which case
    candidates lists what was found (empty if truly nothing matched)."""
    if identifier in graph.nodes:
        return identifier, [identifier]
    candidates = [n for n in graph.nodes if n.rsplit("::", 1)[-1] == identifier]
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def query_blast_radius(graph: nx.DiGraph, function: str, depth: int = 2) -> dict[str, Any]:
    """Who (transitively) calls `function`, up to `depth` hops -- "what
    breaks if I change this." See module docstring for the accuracy ceiling
    (name-based resolution, no type info)."""
    resolved, candidates = resolve_node(graph, function)
    if resolved is None:
        return {"error": f"'{function}' not found or ambiguous", "candidates": candidates}

    callers_by_depth: list[list[str]] = []
    seen = {resolved}
    frontier = [resolved]
    for _ in range(depth):
        next_frontier = []
        for node in frontier:
            for pred in graph.predecessors(node):
                if pred not in seen:
                    seen.add(pred)
                    next_frontier.append(pred)
        if not next_frontier:
            break
        callers_by_depth.append(sorted(next_frontier))
        frontier = next_frontier

    return {
        "function": resolved,
        "callers_by_depth": callers_by_depth,
        "unresolved_calls": graph.graph.get("unresolved_calls", 0),
        "total_calls": graph.graph.get("total_calls", 0),
        "ambiguous_calls": len(graph.graph.get("ambiguous_calls", [])),
    }


def query_shortest_path(graph: nx.DiGraph, source: str, target: str) -> dict[str, Any]:
    """Does `source` ever reach `target` through a chain of calls, and how."""
    resolved_source, source_candidates = resolve_node(graph, source)
    resolved_target, target_candidates = resolve_node(graph, target)
    if resolved_source is None:
        return {"error": f"'{source}' not found or ambiguous", "candidates": source_candidates}
    if resolved_target is None:
        return {"error": f"'{target}' not found or ambiguous", "candidates": target_candidates}

    try:
        path = nx.shortest_path(graph, resolved_source, resolved_target)
    except nx.NetworkXNoPath:
        return {"source": resolved_source, "target": resolved_target, "path": None}
    return {"source": resolved_source, "target": resolved_target, "path": path}


def query_get_cluster(graph: nx.DiGraph, function: str) -> dict[str, Any]:
    """Which Leiden community `function` belongs to, and its other members.

    Clustering quality depends entirely on the underlying graph's structure:
    a small, centralized codebase may legitimately produce one dominant
    cluster or many singletons -- that reflects the codebase, not a broken
    clustering step (see docs/CALL_GRAPH_CLUSTERING.md)."""
    resolved, candidates = resolve_node(graph, function)
    if resolved is None:
        return {"error": f"'{function}' not found or ambiguous", "candidates": candidates}

    clusters = compute_clusters(graph)
    cluster_id = clusters.get(resolved)
    members = sorted(n for n, c in clusters.items() if c == cluster_id)
    return {"function": resolved, "cluster_id": cluster_id, "members": members}


def format_blast_radius_result(result: dict) -> str:
    if "error" in result:
        return format_lookup_error(result)
    lines = [f"Blast radius for {result['function']}:"]
    if not result["callers_by_depth"]:
        lines.append("  (nothing in this repo calls it, directly or transitively)")
    for depth_i, callers in enumerate(result["callers_by_depth"], 1):
        lines.append(f"  depth {depth_i}: " + ", ".join(callers))
    lines.append(
        f"\n(name-based resolution, no type info -- {result['unresolved_calls']}/{result['total_calls']} "
        f"calls in this repo could not be resolved to a known definition, and "
        f"{result['ambiguous_calls']} matched 2+ candidates and were dropped rather than guessed; "
        "see docs/CALL_GRAPH_CLUSTERING.md)"
    )
    return "\n".join(lines)


def format_shortest_path_result(result: dict) -> str:
    if "error" in result:
        return format_lookup_error(result)
    if result["path"] is None:
        return f"No call path found from {result['source']} to {result['target']}."
    return f"{result['source']} -> {result['target']}:\n  " + " -> ".join(result["path"])


def format_cluster_result(result: dict) -> str:
    if "error" in result:
        return format_lookup_error(result)
    lines = [f"{result['function']} is in cluster {result['cluster_id']} ({len(result['members'])} members):"]
    lines.extend(f"  {m}" for m in result["members"])
    return "\n".join(lines)


def format_lookup_error(result: dict) -> str:
    candidates = result.get("candidates") or []
    message = result["error"]
    if candidates:
        return f"{message}, candidates: " + ", ".join(candidates)
    return f"{message} (use a qualified name like 'path/to/file.py::function_name')"
