"""Tests for callgraph.py -- the call-graph extraction + Leiden clustering
spike (docs/CALL_GRAPH_CLUSTERING.md, issue #43). Extraction is built
entirely on CocoIndex's existing match_code() (see docs/findings/2026-08.md,
2026-08-24 entry): no new tree-sitter dependency, no persisted index -- every
build_call_graph() call re-walks the live checkout via chunking.find_code_files(),
exactly like pattern_search_code() does.

Body spans are *approximated* via indentation (CodePattern only reports a
def's header span, not its full body -- see the module docstring in
callgraph.py), so the interesting risk here isn't "does match_code work" (a
plain unit-test concern) but "does the body-span approximation attribute
calls to the right enclosing function." Several test methods below exist
specifically to pin down that behavior for cases discovered while planning
this spike to be genuinely tricky for a naive "nearest preceding def by
start line" heuristic: a call in a nested function's *outer* scope that
comes textually after the *inner* def, a call inside a decorator's own
argument list, and a call sitting at module level between two functions.
"""
from __future__ import annotations

import networkx as nx

from engram import callgraph


class TestExtractDefinitions:
    def test_finds_top_level_function(self):
        src = "def foo():\n    return 1\n"
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        assert [d.qualified_name for d in defs] == ["mod.py::foo"]
        assert defs[0].name == "foo"
        assert defs[0].start_line == 1

    def test_finds_nested_and_method_defs(self):
        src = (
            "def outer():\n"
            "    def inner():\n"
            "        return 1\n"
            "    return inner()\n"
            "\n"
            "class Thing:\n"
            "    def method(self):\n"
            "        return 2\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        names = {d.name for d in defs}
        assert names == {"outer", "inner", "method"}

    def test_ignores_call_shaped_text_that_is_not_a_definition(self):
        src = "result = some_function(1, 2)\n"
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        assert defs == []

    def test_body_end_line_stops_at_next_top_level_def(self):
        src = (
            "def first():\n"
            "    return 1\n"
            "\n"
            "def second():\n"
            "    return 2\n"
        )
        defs = {d.name: d for d in callgraph.extract_definitions(src, "python", "mod.py")}
        # first's body is just line 2; line 4 (def second) must not be
        # included in first's approximated body span.
        assert defs["first"].start_line < defs["first"].body_end_line < defs["second"].start_line

    def test_finds_function_with_return_type_annotation(self):
        """Regression test for a bug found while verifying this module
        against engram's own (heavily-typed) source: `def \\NAME(\\(A*\\)):`
        (with a trailing colon) silently fails to match any function with a
        return-type annotation, since `-> T` sits between the closing paren
        and the colon. The def pattern must not require a bare `):`."""
        src = (
            "def typed(x: int) -> list[dict[str, int]]:\n"
            "    return []\n"
            "\n"
            "def untyped(y):\n"
            "    return typed(y)\n"
        )
        defs = {d.name: d for d in callgraph.extract_definitions(src, "python", "mod.py")}
        assert set(defs) == {"typed", "untyped"}

    def test_multiline_signature_body_starts_after_full_header(self):
        src = (
            "def multiline_sig(\n"
            "    a,\n"
            "    b,\n"
            "):\n"
            "    return a + b\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        assert len(defs) == 1
        # The body (line 5) must be considered inside the function, not cut
        # off right after the first signature line (line 1).
        assert defs[0].start_line == 1
        assert defs[0].body_end_line >= 5

    def test_captures_keyword_capable_param_names(self):
        src = "def f(a, b=1, *args, c, **kwargs):\n    return 1\n"
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        assert defs[0].param_keywords == frozenset({"a", "b", "c"})
        assert defs[0].accepts_arbitrary_keywords is True

    def test_no_var_keyword_param_reports_false(self):
        src = "def f(a, b=1):\n    return 1\n"
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        assert defs[0].param_keywords == frozenset({"a", "b"})
        assert defs[0].accepts_arbitrary_keywords is False

    def test_no_params_gives_empty_keyword_set(self):
        src = "def f():\n    return 1\n"
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        assert defs[0].param_keywords == frozenset()
        assert defs[0].accepts_arbitrary_keywords is False


class TestExtractCallSites:
    def test_distinguishes_call_from_function_definition_kind(self):
        src = (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def caller():\n"
            "    return helper()\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = callgraph.extract_call_sites(src, "python", "mod.py", defs)
        # Exactly one real call site (helper() inside caller) -- the
        # function_definition/argument_list matches for the same textual
        # shape must not leak through as call edges.
        assert len(calls) == 1
        assert calls[0].callee_name == "helper"

    def test_resolves_caller_for_simple_case(self):
        src = (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def caller():\n"
            "    return helper()\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = callgraph.extract_call_sites(src, "python", "mod.py", defs)
        assert calls[0].caller == "mod.py::caller"

    def test_nested_function_call_after_inner_def_attributed_to_outer_not_inner(self):
        """The naive 'nearest preceding def by start line' heuristic gets
        this wrong: inner() starts on line 2 (closer to line 4 than outer's
        line 1), but `return inner()` on line 4 is textually back in
        outer's body, not inner's. Containment via the approximated body
        span must attribute this call to outer."""
        src = (
            "def outer():\n"
            "    def inner():\n"
            "        return 1\n"
            "    return inner()\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = callgraph.extract_call_sites(src, "python", "mod.py", defs)
        assert len(calls) == 1
        assert calls[0].callee_name == "inner"
        assert calls[0].caller == "mod.py::outer"

    def test_call_inside_nested_inner_function_attributed_to_inner(self):
        src = (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def outer():\n"
            "    def inner():\n"
            "        return helper()\n"
            "    return inner()\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = {c.callee_name: c for c in callgraph.extract_call_sites(src, "python", "mod.py", defs)}
        assert calls["helper"].caller == "mod.py::inner"
        assert calls["inner"].caller == "mod.py::outer"

    def test_call_in_decorator_argument_resolves_to_none_not_previous_function(self):
        """Known-unknown edge case #1 (docs/CALL_GRAPH_CLUSTERING.md): a call
        inside a decorator's own argument list sits textually between the
        previous function and the decorated one, but isn't inside either
        function's body. A naive line-proximity heuristic would misattribute
        it to the previous function; containment must resolve it to None
        instead (module/class-body-level, not "inside" any function)."""
        src = (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "@some_decorator(helper())\n"
            "def caller():\n"
            "    return 2\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = [c for c in callgraph.extract_call_sites(src, "python", "mod.py", defs) if c.callee_name == "helper"]
        assert len(calls) == 1
        assert calls[0].caller is None

    def test_call_after_multiline_signature_attributed_correctly(self):
        """Known-unknown edge case #2: a call inside a function whose own
        signature spans multiple lines must still resolve to that function,
        not fall outside its (correctly computed) body span."""
        src = (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def multiline_sig(\n"
            "    a,\n"
            "    b,\n"
            "):\n"
            "    return helper()\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = [c for c in callgraph.extract_call_sites(src, "python", "mod.py", defs) if c.callee_name == "helper"]
        assert len(calls) == 1
        assert calls[0].caller == "mod.py::multiline_sig"

    def test_module_level_call_between_functions_resolves_to_none(self):
        """Known-unknown edge case #3: a call at module level, sitting
        textually between two function defs, must not be misattributed to
        whichever function happens to precede it."""
        src = (
            "def first():\n"
            "    return 1\n"
            "\n"
            "result = first()\n"
            "\n"
            "def second():\n"
            "    return 2\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = [c for c in callgraph.extract_call_sites(src, "python", "mod.py", defs) if c.callee_name == "first"]
        assert len(calls) == 1
        assert calls[0].caller is None

    def test_call_before_any_definition_resolves_to_none(self):
        src = "bootstrap()\n\ndef main():\n    return 1\n"
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = callgraph.extract_call_sites(src, "python", "mod.py", defs)
        assert calls[0].caller is None

    def test_captures_keyword_argument_names(self):
        src = (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def caller():\n"
            "    return helper(1, repo='x', limit=10)\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = callgraph.extract_call_sites(src, "python", "mod.py", defs)
        assert calls[0].keyword_args == frozenset({"repo", "limit"})

    def test_forwarded_kwargs_not_treated_as_named_keyword(self):
        """`**kw` forwarding can't be resolved statically -- it must not be
        parsed as a keyword argument named "kw"."""
        src = (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def caller(**kw):\n"
            "    return helper(**kw)\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = callgraph.extract_call_sites(src, "python", "mod.py", defs)
        assert calls[0].keyword_args == frozenset()

    def test_equality_comparison_argument_not_treated_as_keyword(self):
        """Regression guard: `helper(x == 1)` must not be misparsed as a
        keyword argument named "x" (an early naive `"=" in part` check
        would have matched the first `=` inside `==`)."""
        src = (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def caller(x):\n"
            "    return helper(x == 1)\n"
        )
        defs = callgraph.extract_definitions(src, "python", "mod.py")
        calls = callgraph.extract_call_sites(src, "python", "mod.py", defs)
        assert calls[0].keyword_args == frozenset()


class TestIsArrowFunctionConst:
    """Unit tests for the filter that narrows `const \\NAME = \\BODY` matches
    down to real arrow-function components (docs/CALL_GRAPH_CLUSTERING.md,
    2026-08-24 Phase 2 entry)."""

    def test_accepts_plain_arrow_with_parens(self):
        assert callgraph._is_arrow_function_const("Foo", "() => { return 1; }")

    def test_accepts_destructured_param_arrow(self):
        assert callgraph._is_arrow_function_const("Foo", "({ filters }: Props) => { return 1; }")

    def test_accepts_single_bare_param_arrow(self):
        assert callgraph._is_arrow_function_const("handler", "cat => setFilter('type', [cat])")

    def test_accepts_async_arrow(self):
        assert callgraph._is_arrow_function_const("runCleanup", "async () => { await x(); }")

    def test_rejects_plain_data_literal(self):
        assert not callgraph._is_arrow_function_const("DEFAULT_TIMEOUT", "60")

    def test_rejects_call_expression_value(self):
        """A common false-positive source: the const's own value is a call
        whose *arguments* happen to contain a `=>` deep inside (e.g. a
        factory taking a callback prop) -- the const itself isn't a
        function."""
        assert not callgraph._is_arrow_function_const(
            "SidebarContent", "NavContentBlueprint.make({ component: ({ navItems }) => null })"
        )

    def test_rejects_destructuring_assignment_name(self):
        """tree-sitter's NAME capture on a destructuring const binds the
        literal `{ t }`/`[a, b]` text, not a real identifier -- must be
        rejected even though the RHS looks function-shaped."""
        assert not callgraph._is_arrow_function_const("{ t }", "useTranslation()")
        assert not callgraph._is_arrow_function_const("[a, b]", "useState(0)")


class TestGrammarForPath:
    def test_ts_file_uses_typescript_grammar(self):
        assert callgraph._grammar_for_path("src/foo.ts", "typescript") == "typescript"

    def test_tsx_file_uses_tsx_grammar(self):
        """The load-bearing case: a `.tsx` file must resolve to the "tsx"
        grammar, not the "typescript" config key it's registered under --
        feeding JSX content to the plain "typescript" grammar (no JSX
        support) silently corrupts extraction from the first JSX construct
        onward (see docs/CALL_GRAPH_CLUSTERING.md, 2026-08-24 Phase 2 entry)."""
        assert callgraph._grammar_for_path("src/Foo.tsx", "typescript") == "tsx"

    def test_python_file_uses_python_grammar(self):
        assert callgraph._grammar_for_path("mod.py", "python") == "python"


class TestExtractDefinitionsTypeScript:
    def test_finds_function_declaration(self):
        src = "function foo(a, b) {\n  return a + b;\n}\n"
        defs = callgraph.extract_definitions(src, "typescript", "mod.ts")
        assert [d.name for d in defs] == ["foo"]
        assert defs[0].start_line == 1

    def test_finds_class_method(self):
        src = "class Foo {\n  bar(x) {\n    return x;\n  }\n}\n"
        defs = callgraph.extract_definitions(src, "typescript", "mod.ts")
        assert [d.name for d in defs] == ["bar"]

    def test_finds_arrow_function_const_in_tsx_file(self):
        src = (
            "export const Widget = ({ items }: Props) => {\n"
            "  return <div>{items.length}</div>;\n"
            "};\n"
        )
        defs = callgraph.extract_definitions(src, "typescript", "Widget.tsx")
        assert [d.name for d in defs] == ["Widget"]

    def test_excludes_plain_data_const(self):
        src = "const TIMEOUT = 60;\nconst NAME = 'x';\n"
        defs = callgraph.extract_definitions(src, "typescript", "mod.ts")
        assert defs == []

    def test_body_end_line_is_exact_not_approximated(self):
        """Unlike Python, a TS def's `\\BODY` capture spans its real,
        parsed extent -- verify it lands exactly on the closing brace, not
        merely "close enough" the way indentation approximation would be."""
        src = "function foo() {\n  return 1;\n}\n\nfunction bar() {\n  return 2;\n}\n"
        defs = {d.name: d for d in callgraph.extract_definitions(src, "typescript", "mod.ts")}
        assert defs["foo"].body_end_line == 3
        assert defs["bar"].body_end_line == 7

    def test_two_sibling_components_in_same_tsx_file_both_found(self):
        """Regression guard for the Phase 2 root cause: two arrow-function
        components in the same real-shaped .tsx file, with JSX in between,
        must both be found once the correct "tsx" grammar is used (this
        looked like a match_code bug under the wrong "typescript" grammar --
        see docs/CALL_GRAPH_CLUSTERING.md, 2026-08-24 entry)."""
        src = (
            "const First = () => {\n"
            "  return (\n"
            "    <div>\n"
            "      <Card onPick={\n"
            "        enabled\n"
            "          ? cat => pick(cat)\n"
            "          : undefined\n"
            "      } />\n"
            "    </div>\n"
            "  );\n"
            "};\n"
            "\n"
            "export const Second = () => {\n"
            "  return <div>ok</div>;\n"
            "};\n"
        )
        defs = callgraph.extract_definitions(src, "typescript", "Page.tsx")
        assert {d.name for d in defs} == {"First", "Second"}

    def test_return_typed_function_declaration_gets_correct_body_end_line(self):
        """Regression test for a latent bug that predates this test (present
        since Phase 2 shipped, only caught during the Phase 3/Rust preflight
        that re-exercised the same \\BODY-capture mechanism): a def pattern
        with no return-type placeholder doesn't just skip a def that has an
        explicit return-type annotation, it *matches* with \\BODY bound to
        the annotation text (`": number"`) instead of the real block --
        silently corrupting body_end_line rather than merely missing the
        def. Fixed by adding a return-type-aware twin spec per brace-bodied
        shape, plus a `_looks_like_real_block_body` guard that discards
        whichever twin's capture doesn't actually look like a block. See
        docs/CALL_GRAPH_CLUSTERING.md, 2026-08-24 Phase 3 entry."""
        src = "function foo(): number {\n  return 1;\n}\n"
        defs = callgraph.extract_definitions(src, "typescript", "mod.ts")
        assert [d.name for d in defs] == ["foo"]
        assert defs[0].body_end_line == 3

    def test_return_typed_class_method_gets_correct_body_end_line(self):
        src = "class Foo {\n  bar(x): number {\n    return x;\n  }\n}\n"
        defs = callgraph.extract_definitions(src, "typescript", "mod.ts")
        assert [d.name for d in defs] == ["bar"]
        assert defs[0].body_end_line == 4

    def test_generic_function_declaration_is_found_exactly_once(self):
        """Regression guard against double-counting: a generic function
        also structurally matches the plain (non-generic) pattern's kind
        filter check at the AST level, but that plain pattern's own \\BODY
        capture for a generic def doesn't look like a real block (garbage
        NAME/BODY from the shape mismatch -- see extract_definitions'
        docstring) and must be filtered out, leaving exactly one entry."""
        src = "function identity<T>(x: T): T {\n  return x;\n}\n"
        defs = callgraph.extract_definitions(src, "typescript", "mod.ts")
        assert len(defs) == 1
        assert defs[0].name == "identity"
        assert defs[0].body_end_line == 3

    def test_generic_class_method_is_found_exactly_once(self):
        src = "class Box {\n  identity<T>(x: T): T {\n    return x;\n  }\n}\n"
        defs = callgraph.extract_definitions(src, "typescript", "mod.ts")
        assert len(defs) == 1
        assert defs[0].name == "identity"
        assert defs[0].body_end_line == 4


class TestExtractCallSitesTypeScript:
    def test_finds_call_expression_and_attributes_to_enclosing_function(self):
        src = (
            "function helper() {\n"
            "  return 1;\n"
            "}\n"
            "\n"
            "function caller() {\n"
            "  return helper();\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "typescript", "mod.ts")
        calls = callgraph.extract_call_sites(src, "typescript", "mod.ts", defs)
        assert len(calls) == 1
        assert calls[0].callee_name == "helper"
        assert calls[0].caller == "mod.ts::caller"

    def test_method_call_and_arrow_const_call_both_resolve(self):
        src = (
            "export const Widget = () => {\n"
            "  return helper();\n"
            "};\n"
            "\n"
            "function helper() {\n"
            "  return 1;\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "typescript", "Widget.tsx")
        calls = callgraph.extract_call_sites(src, "typescript", "Widget.tsx", defs)
        assert len(calls) == 1
        assert calls[0].callee_name == "helper"
        assert calls[0].caller == "Widget.tsx::Widget"


class TestBuildCallGraph:
    def _make_tree(self, tmp_path):
        (tmp_path / "a.py").write_text(
            "def shared_helper():\n"
            "    return 1\n"
            "\n"
            "def uses_helper_locally():\n"
            "    return shared_helper()\n"
        )
        (tmp_path / "b.py").write_text(
            "import a\n"
            "\n"
            "def uses_helper_cross_file():\n"
            "    return a.shared_helper()\n"
            "\n"
            "def calls_unknown():\n"
            "    return totally_unknown_function()\n"
        )
        return tmp_path

    def test_builds_nodes_and_edges_across_files(self, tmp_path):
        root = self._make_tree(tmp_path)
        graph = callgraph.build_call_graph(root, included=["**/*.py"], excluded=[], language="python")

        assert "a.py::shared_helper" in graph.nodes
        assert "a.py::uses_helper_locally" in graph.nodes
        assert "b.py::uses_helper_cross_file" in graph.nodes
        assert graph.has_edge("a.py::uses_helper_locally", "a.py::shared_helper")

    def test_dotted_call_name_resolved_by_trailing_segment(self, tmp_path):
        root = self._make_tree(tmp_path)
        graph = callgraph.build_call_graph(root, included=["**/*.py"], excluded=[], language="python")

        # b.py calls a.shared_helper() -- resolved by the trailing "shared_helper"
        # segment against the known qualified name, despite living in a
        # different file (this is the whole point of building a repo-wide graph).
        assert graph.has_edge("b.py::uses_helper_cross_file", "a.py::shared_helper")

    def test_unresolved_calls_are_counted_not_fatal(self, tmp_path):
        root = self._make_tree(tmp_path)
        graph = callgraph.build_call_graph(root, included=["**/*.py"], excluded=[], language="python")

        assert "b.py::calls_unknown" in graph.nodes
        assert not any(graph.successors("b.py::calls_unknown"))
        assert graph.graph["unresolved_calls"] >= 1

    def test_same_file_call_prefers_same_file_target_over_other_files(self, tmp_path):
        """Regression test for a bug found while verifying this module
        against engram's own several near-identical *_code_pattern_search
        modules: a same-file call to a common helper name (e.g. a private
        `_run_it()` each file defines its own copy of) must resolve to that
        file's own definition, not fan out to every same-named function
        across the whole repo."""
        (tmp_path / "one.py").write_text(
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def main():\n"
            "    return helper()\n"
        )
        (tmp_path / "two.py").write_text(
            "def helper():\n"
            "    return 2\n"
        )
        graph = callgraph.build_call_graph(tmp_path, included=["**/*.py"], excluded=[], language="python")

        assert graph.has_edge("one.py::main", "one.py::helper")
        assert not graph.has_edge("one.py::main", "two.py::helper")

    def test_missing_root_returns_empty_graph_not_error(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        graph = callgraph.build_call_graph(missing, included=["**/*.py"], excluded=[], language="python")
        assert list(graph.nodes) == []

    def test_signature_incompatible_candidate_filtered_out(self, tmp_path):
        """Regression test for the Serena cross-check's dominant false
        positive (docs/CALL_GRAPH_CLUSTERING.md): a same-named function in
        another file that doesn't accept a keyword the call passes must be
        filtered out, leaving the one compatible candidate as a confirmed
        edge -- not a same-file match, so this exercises signature filtering
        specifically, not the same-file preference."""
        (tmp_path / "caller.py").write_text(
            "import one\n"
            "import two\n"
            "\n"
            "def main():\n"
            "    return one.run(repo='x')\n"
        )
        (tmp_path / "one.py").write_text(
            "def run(repo=None):\n"
            "    return repo\n"
        )
        (tmp_path / "two.py").write_text(
            "def run(other=None):\n"
            "    return other\n"
        )
        graph = callgraph.build_call_graph(tmp_path, included=["**/*.py"], excluded=[], language="python")

        assert graph.has_edge("caller.py::main", "one.py::run")
        assert not graph.has_edge("caller.py::main", "two.py::run")
        assert graph.graph["ambiguous_calls"] == []

    def test_genuinely_ambiguous_call_surfaced_not_silently_added_or_dropped(self, tmp_path):
        """Graphify-style policy: when signature filtering still leaves 2+
        viable candidates (same bare name, same-compatible signature, no
        same-file match), no edge is guessed -- the call is recorded in
        `ambiguous_calls` instead of vanishing or picking an arbitrary
        target."""
        (tmp_path / "caller.py").write_text(
            "import one\n"
            "\n"
            "def main():\n"
            "    return one.run()\n"
        )
        (tmp_path / "one.py").write_text(
            "def run():\n"
            "    return 1\n"
        )
        (tmp_path / "two.py").write_text(
            "def run():\n"
            "    return 2\n"
        )
        graph = callgraph.build_call_graph(tmp_path, included=["**/*.py"], excluded=[], language="python")

        assert not graph.has_edge("caller.py::main", "one.py::run")
        assert not graph.has_edge("caller.py::main", "two.py::run")
        ambiguous = graph.graph["ambiguous_calls"]
        assert len(ambiguous) == 1
        assert ambiguous[0]["caller"] == "caller.py::main"
        assert ambiguous[0]["callee_name"] == "one.run"
        assert set(ambiguous[0]["candidates"]) == {"one.py::run", "two.py::run"}

    def test_signature_filtering_eliminating_lone_candidate_still_resolves(self, tmp_path):
        """If the (heuristic, non-parser) keyword filter is wrong for some
        edge case and rejects the only known candidate, that must not
        silently turn a real match into "unresolved" or "ambiguous" -- with
        only one candidate to begin with, the fallback-to-unfiltered-pool
        still resolves to it as a confirmed edge."""
        (tmp_path / "caller.py").write_text(
            "import one\n"
            "\n"
            "def main():\n"
            "    return one.run(unexpected_kw=1)\n"
        )
        (tmp_path / "one.py").write_text(
            "def run():\n"
            "    return 1\n"
        )
        graph = callgraph.build_call_graph(tmp_path, included=["**/*.py"], excluded=[], language="python")

        assert graph.has_edge("caller.py::main", "one.py::run")
        assert graph.graph["unresolved_calls"] == 0
        assert graph.graph["ambiguous_calls"] == []


class TestComputeClusters:
    def test_two_disconnected_groups_get_different_clusters(self):
        graph = nx.DiGraph()
        graph.add_edge("group1::a", "group1::b")
        graph.add_edge("group1::b", "group1::a")
        graph.add_edge("group2::c", "group2::d")
        graph.add_edge("group2::d", "group2::c")

        clusters = callgraph.compute_clusters(graph)

        assert clusters["group1::a"] == clusters["group1::b"]
        assert clusters["group2::c"] == clusters["group2::d"]
        assert clusters["group1::a"] != clusters["group2::c"]

    def test_single_connected_component_gets_one_cluster(self):
        graph = nx.DiGraph()
        graph.add_edge("mod.py::a", "mod.py::b")
        graph.add_edge("mod.py::b", "mod.py::c")

        clusters = callgraph.compute_clusters(graph)

        assert len(set(clusters.values())) == 1


class TestBuildCallGraphWithStats:
    def test_returns_same_graph_as_build_call_graph(self, tmp_path):
        (tmp_path / "a.py").write_text("def f():\n    return 1\n")

        graph = callgraph.build_call_graph_with_stats(
            tmp_path, included=["**/*.py"], excluded=[], language="python",
        )

        assert "a.py::f" in graph.nodes

    def test_logs_to_the_passed_in_logger_not_the_module_default(self, tmp_path, caplog):
        import logging

        (tmp_path / "a.py").write_text("def f():\n    return 1\n")
        custom_logger = logging.getLogger("some-org-search")

        with caplog.at_level(logging.INFO, logger="some-org-search"):
            callgraph.build_call_graph_with_stats(
                tmp_path, included=["**/*.py"], excluded=[], language="python", logger=custom_logger,
            )

        assert any("call graph built in" in r.message for r in caplog.records)
        assert all(r.name == "some-org-search" for r in caplog.records)


class TestResolveNode:
    def _graph(self):
        graph = nx.DiGraph()
        graph.add_node("a.py::foo")
        graph.add_node("b.py::foo")
        graph.add_node("a.py::bar")
        return graph

    def test_exact_qualified_name_resolves_directly(self):
        resolved, candidates = callgraph.resolve_node(self._graph(), "a.py::bar")
        assert resolved == "a.py::bar"
        assert candidates == ["a.py::bar"]

    def test_unambiguous_bare_name_resolves(self):
        resolved, candidates = callgraph.resolve_node(self._graph(), "bar")
        assert resolved == "a.py::bar"
        assert candidates == ["a.py::bar"]

    def test_ambiguous_bare_name_returns_none_with_candidates(self):
        resolved, candidates = callgraph.resolve_node(self._graph(), "foo")
        assert resolved is None
        assert set(candidates) == {"a.py::foo", "b.py::foo"}

    def test_unknown_name_returns_none_with_empty_candidates(self):
        resolved, candidates = callgraph.resolve_node(self._graph(), "nope")
        assert resolved is None
        assert candidates == []


class TestQueryBlastRadius:
    def _graph(self):
        graph = nx.DiGraph()
        graph.add_edge("a.py::caller", "a.py::callee")
        graph.add_edge("a.py::grandcaller", "a.py::caller")
        graph.graph["unresolved_calls"] = 1
        graph.graph["total_calls"] = 5
        graph.graph["ambiguous_calls"] = [{"caller": "x"}]
        return graph

    def test_unknown_function_returns_error(self):
        result = callgraph.query_blast_radius(self._graph(), "does_not_exist")
        assert "error" in result

    def test_direct_and_transitive_callers_grouped_by_depth(self):
        result = callgraph.query_blast_radius(self._graph(), "callee", depth=2)
        assert result["function"] == "a.py::callee"
        assert result["callers_by_depth"] == [["a.py::caller"], ["a.py::grandcaller"]]

    def test_depth_limits_how_far_the_search_goes(self):
        result = callgraph.query_blast_radius(self._graph(), "callee", depth=1)
        assert result["callers_by_depth"] == [["a.py::caller"]]

    def test_stats_are_passed_through_from_graph_metadata(self):
        result = callgraph.query_blast_radius(self._graph(), "callee")
        assert result["unresolved_calls"] == 1
        assert result["total_calls"] == 5
        assert result["ambiguous_calls"] == 1

    def test_leaf_function_has_no_callers(self):
        graph = nx.DiGraph()
        graph.add_node("a.py::lonely")
        result = callgraph.query_blast_radius(graph, "lonely")
        assert result["callers_by_depth"] == []


class TestQueryShortestPath:
    def _graph(self):
        graph = nx.DiGraph()
        graph.add_edge("a.py::start", "a.py::middle")
        graph.add_edge("a.py::middle", "a.py::end")
        graph.add_node("a.py::disconnected")
        return graph

    def test_unknown_source_returns_error(self):
        result = callgraph.query_shortest_path(self._graph(), "nope", "end")
        assert "error" in result

    def test_unknown_target_returns_error(self):
        result = callgraph.query_shortest_path(self._graph(), "start", "nope")
        assert "error" in result

    def test_reachable_target_returns_full_path(self):
        result = callgraph.query_shortest_path(self._graph(), "start", "end")
        assert result["path"] == ["a.py::start", "a.py::middle", "a.py::end"]

    def test_unreachable_target_returns_none_path_not_error(self):
        result = callgraph.query_shortest_path(self._graph(), "start", "disconnected")
        assert result["path"] is None
        assert "error" not in result


class TestQueryGetCluster:
    def test_unknown_function_returns_error(self):
        graph = nx.DiGraph()
        graph.add_node("a.py::only")
        result = callgraph.query_get_cluster(graph, "does_not_exist")
        assert "error" in result

    def test_connected_functions_share_a_cluster(self):
        graph = nx.DiGraph()
        graph.add_edge("a.py::x", "a.py::y")
        result = callgraph.query_get_cluster(graph, "x")
        assert result["function"] == "a.py::x"
        assert set(result["members"]) == {"a.py::x", "a.py::y"}


class TestFormatBlastRadiusResult:
    def test_error_result_delegates_to_format_lookup_error(self):
        out = callgraph.format_blast_radius_result({"error": "boom", "candidates": []})
        assert "boom" in out

    def test_no_callers_notes_nothing_calls_it(self):
        out = callgraph.format_blast_radius_result({
            "function": "a.py::lonely", "callers_by_depth": [],
            "unresolved_calls": 0, "total_calls": 0, "ambiguous_calls": 0,
        })
        assert "nothing in this repo calls it" in out

    def test_callers_by_depth_are_rendered_per_depth(self):
        out = callgraph.format_blast_radius_result({
            "function": "a.py::callee", "callers_by_depth": [["a.py::caller"]],
            "unresolved_calls": 2, "total_calls": 10, "ambiguous_calls": 1,
        })
        assert "depth 1: a.py::caller" in out
        assert "2/10" in out
        assert "1 matched 2+ candidates" in out


class TestFormatShortestPathResult:
    def test_error_result_delegates_to_format_lookup_error(self):
        out = callgraph.format_shortest_path_result({"error": "boom", "candidates": []})
        assert "boom" in out

    def test_no_path_gives_friendly_message(self):
        out = callgraph.format_shortest_path_result({"source": "a", "target": "b", "path": None})
        assert "No call path found from a to b" in out

    def test_path_is_rendered_as_arrow_chain(self):
        out = callgraph.format_shortest_path_result({
            "source": "a.py::start", "target": "a.py::end",
            "path": ["a.py::start", "a.py::mid", "a.py::end"],
        })
        assert "a.py::start -> a.py::mid -> a.py::end" in out


class TestFormatClusterResult:
    def test_error_result_delegates_to_format_lookup_error(self):
        out = callgraph.format_cluster_result({"error": "boom", "candidates": []})
        assert "boom" in out

    def test_lists_all_members(self):
        out = callgraph.format_cluster_result({
            "function": "a.py::x", "cluster_id": 3, "members": ["a.py::x", "a.py::y"],
        })
        assert "cluster 3" in out
        assert "a.py::x" in out
        assert "a.py::y" in out


class TestExtractDefinitionsRust:
    def test_finds_plain_function(self):
        src = "fn plain_fn(x: i32) -> i32 {\n    x\n}\n"
        defs = callgraph.extract_definitions(src, "rust", "lib.rs")
        assert [d.name for d in defs] == ["plain_fn"]

    def test_finds_generic_function_and_lifetime_function(self):
        """Regression guard: a literal `<`/`>` in the pattern only matches a
        fn that actually has type params or lifetimes, and the plain
        no-angle-bracket pattern never matches one that does (verified via
        cocoindex's match_code directly -- see docs/CALL_GRAPH_CLUSTERING.md,
        2026-08-24 Phase 3 entry) -- so rust needs both DefSpecs, and this
        pins down that both fire on the shapes they're each meant for."""
        src = (
            "fn generic_fn<T: Clone>(x: T) -> T {\n    x.clone()\n}\n\n"
            "fn with_lifetime<'a>(x: &'a str) -> &'a str {\n    x\n}\n"
        )
        defs = callgraph.extract_definitions(src, "rust", "lib.rs")
        assert {d.name for d in defs} == {"generic_fn", "with_lifetime"}

    def test_finds_methods_inside_impl_block(self):
        src = (
            "struct Foo { x: i32 }\n\n"
            "impl Foo {\n"
            "    fn new(x: i32) -> Self {\n"
            "        Foo { x }\n"
            "    }\n\n"
            "    pub fn get(&self) -> i32 {\n"
            "        self.x\n"
            "    }\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "rust", "foo.rs")
        assert {d.name for d in defs} == {"new", "get"}

    def test_finds_trait_impl_method_but_not_bodyless_trait_signature(self):
        src = (
            "trait Greet {\n"
            "    fn hello(&self) -> String;\n"
            "}\n\n"
            "impl Greet for Foo {\n"
            "    fn hello(&self) -> String {\n"
            "        format!(\"hi\")\n"
            "    }\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "rust", "foo.rs")
        # Exactly one `hello` -- the trait's own bodyless signature is kind
        # `function_signature_item`, not `function_item`, and is correctly
        # excluded rather than double-counted with the impl's real one.
        assert [d.name for d in defs] == ["hello"]

    def test_where_clause_function_is_dropped_not_misattributed(self):
        """Known, deliberate scope limitation, not a bug: a `where` clause
        sits between the return type and the body, a third pattern axis
        beyond the {generic, return-type} 2x2 this module already handles
        (see _DEF_SPECS's module comment) -- none of Rust's 4 registered
        specs has a `where` placeholder, so `\\BODY` would otherwise bind to
        the `where` clause's own text instead of the real block.
        `_looks_like_real_block_body` catches and drops this (its text
        starts with "where", not "{") rather than keeping a misattributed
        entry. Measured against the real praxis-proxy checkouts
        (2026-08-24): `where` clauses appear in ~0.37% of function-shaped
        lines, an acceptable recall gap for not quadrupling the spec count
        again -- see docs/CALL_GRAPH_CLUSTERING.md, Phase 3 entry."""
        src = (
            "fn where_clause_fn<T>(x: T) -> T\n"
            "where\n"
            "    T: Clone + Send,\n"
            "{\n"
            "    x.clone()\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "rust", "lib.rs")
        assert defs == []


class TestExtractCallSitesRust:
    def test_free_function_call(self):
        src = (
            "fn helper(y: i32) -> i32 {\n    y * 2\n}\n\n"
            "fn caller() {\n    helper(1);\n}\n"
        )
        defs = callgraph.extract_definitions(src, "rust", "lib.rs")
        calls = callgraph.extract_call_sites(src, "rust", "lib.rs", defs)
        assert len(calls) == 1
        assert calls[0].callee_name == "helper"
        assert calls[0].caller == "lib.rs::caller"

    def test_method_call_captures_receiver_qualified_name(self):
        """`self.bar()`/`f.bar()` are call_expression nodes whose NAME
        capture is the whole `receiver.method` text, not just `method` --
        build_call_graph()'s bare-callee trailing-segment split (tested
        separately) is what turns this into a resolvable name, not this
        extraction step."""
        src = (
            "impl Foo {\n"
            "    fn bar(&self) -> i32 {\n"
            "        self.x\n"
            "    }\n\n"
            "    fn baz(&self) -> i32 {\n"
            "        self.bar()\n"
            "    }\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "rust", "foo.rs")
        calls = callgraph.extract_call_sites(src, "rust", "foo.rs", defs)
        assert len(calls) == 1
        assert calls[0].callee_name == "self.bar"
        assert calls[0].caller == "foo.rs::baz"

    def test_associated_function_call_uses_double_colon(self):
        src = (
            "struct Foo;\n\n"
            "impl Foo {\n"
            "    fn new() -> Self {\n"
            "        Foo\n"
            "    }\n"
            "}\n\n"
            "fn caller() {\n"
            "    Foo::new();\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "rust", "foo.rs")
        calls = callgraph.extract_call_sites(src, "rust", "foo.rs", defs)
        assert len(calls) == 1
        assert calls[0].callee_name == "Foo::new"
        assert calls[0].caller == "foo.rs::caller"


class TestBuildCallGraphRust:
    def test_associated_function_call_resolves_via_double_colon_split(self, tmp_path):
        """Regression test for the bare-callee split fix: without also
        splitting on `::`, `Foo::new()` would never resolve (`.` alone
        leaves the whole "Foo::new" string unsplit, which never matches
        name_index's bare "new" key)."""
        (tmp_path / "foo.rs").write_text(
            "struct Foo;\n\n"
            "impl Foo {\n"
            "    fn new() -> Self {\n"
            "        Foo\n"
            "    }\n"
            "}\n\n"
            "fn caller() {\n"
            "    Foo::new();\n"
            "}\n"
        )
        graph = callgraph.build_call_graph(tmp_path, included=["**/*.rs"], excluded=[], language="rust")
        assert graph.has_edge("foo.rs::caller", "foo.rs::new")

    def test_method_call_resolves_via_dot_split(self, tmp_path):
        (tmp_path / "foo.rs").write_text(
            "impl Foo {\n"
            "    fn bar(&self) -> i32 {\n"
            "        self.x\n"
            "    }\n\n"
            "    fn baz(&self) -> i32 {\n"
            "        self.bar()\n"
            "    }\n"
            "}\n"
        )
        graph = callgraph.build_call_graph(tmp_path, included=["**/*.rs"], excluded=[], language="rust")
        assert graph.has_edge("foo.rs::baz", "foo.rs::bar")


class TestExtractDefinitionsGo:
    def test_finds_plain_function(self):
        src = "func plain() {\n    doWork()\n}\n"
        defs = callgraph.extract_definitions(src, "go", "main.go")
        assert [d.name for d in defs] == ["plain"]

    def test_return_typed_function_gets_correct_body_end_line(self):
        """Same latent-bug class as TS/Rust (see docs/CALL_GRAPH_CLUSTERING.md,
        2026-08-24 Phase 2/3 entries): a pattern with no return-type
        placeholder matches a return-typed Go func too, but with \\BODY
        bound to the return-type text (`"error"`) instead of the real
        block -- the return-type-aware twin spec plus
        _looks_like_real_block_body together fix this, same as for TS/Rust."""
        src = "func withReturn() error {\n    return nil\n}\n"
        defs = callgraph.extract_definitions(src, "go", "main.go")
        assert [d.name for d in defs] == ["withReturn"]
        assert defs[0].body_end_line == 3

    def test_multi_value_return_type_gets_correct_body_end_line(self):
        src = "func multiReturn() (int, error) {\n    return 1, nil\n}\n"
        defs = callgraph.extract_definitions(src, "go", "main.go")
        assert [d.name for d in defs] == ["multiReturn"]
        assert defs[0].body_end_line == 3

    def test_named_return_values_get_correct_body_end_line(self):
        src = "func namedReturns() (result int, err error) {\n    return 1, nil\n}\n"
        defs = callgraph.extract_definitions(src, "go", "main.go")
        assert [d.name for d in defs] == ["namedReturns"]
        assert defs[0].body_end_line == 3

    def test_generic_function_is_found_exactly_once(self):
        """Go generics use `[T any]`, not `<T>` -- same mutual-exclusion
        behavior as TS/Rust's angle-bracket generics: a pattern with a
        literal `[`/`]` only matches a func that has type params, and the
        plain pattern's match against a generic func (if any) is filtered
        out by _looks_like_real_block_body, leaving exactly one entry."""
        src = "func Generic[T any](x T) T {\n    return x\n}\n"
        defs = callgraph.extract_definitions(src, "go", "main.go")
        assert len(defs) == 1
        assert defs[0].name == "Generic"
        assert defs[0].body_end_line == 3

    def test_finds_method_with_pointer_receiver(self):
        src = (
            "type Server struct{}\n\n"
            "func (s *Server) Handle() error {\n"
            "    return nil\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "go", "server.go")
        assert [d.name for d in defs] == ["Handle"]
        assert defs[0].body_end_line == 5

    def test_method_with_generic_receiver_is_found_via_plain_receiver_capture(self):
        """A method can't have its own type params in Go -- only its
        receiver type can be generic -- and that's already fully inside the
        `\\(R*\\)` receiver capture, so no separate generic spec is needed
        for method_declaration (unlike function_declaration)."""
        src = (
            "func (s *Server[T]) Method(x T) T {\n"
            "    return x\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "go", "server.go")
        assert [d.name for d in defs] == ["Method"]
        assert defs[0].body_end_line == 3

    def test_interface_method_signature_is_excluded(self):
        """A bodyless interface method is kind `method_elem`, distinct from
        a real `method_declaration` -- naturally excluded by the kind
        filter, not double-counted alongside a real impl."""
        src = (
            "type Greeter interface {\n"
            "    Greet() string\n"
            "}\n\n"
            "type Foo struct{}\n\n"
            "func (f *Foo) Greet() string {\n"
            "    return \"hi\"\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "go", "greet.go")
        assert [d.name for d in defs] == ["Greet"]


class TestExtractCallSitesGo:
    def test_free_function_call(self):
        src = "func helper() int {\n    return 2\n}\n\nfunc caller() int {\n    return helper()\n}\n"
        defs = callgraph.extract_definitions(src, "go", "main.go")
        calls = callgraph.extract_call_sites(src, "go", "main.go", defs)
        assert len(calls) == 1
        assert calls[0].callee_name == "helper"
        assert calls[0].caller == "main.go::caller"

    def test_method_call_and_package_qualified_call_both_captured(self):
        src = (
            "func caller() {\n"
            "    obj.Method()\n"
            "    pkg.Func()\n"
            "}\n"
        )
        defs = callgraph.extract_definitions(src, "go", "main.go")
        calls = callgraph.extract_call_sites(src, "go", "main.go", defs)
        names = {c.callee_name for c in calls}
        assert names == {"obj.Method", "pkg.Func"}
        assert all(c.caller == "main.go::caller" for c in calls)


class TestBuildCallGraphGo:
    def test_method_call_resolves_via_dot_split(self, tmp_path):
        (tmp_path / "server.go").write_text(
            "type Server struct{}\n\n"
            "func (s *Server) Handle() error {\n"
            "    return s.internal()\n"
            "}\n\n"
            "func (s *Server) internal() error {\n"
            "    return nil\n"
            "}\n"
        )
        graph = callgraph.build_call_graph(tmp_path, included=["**/*.go"], excluded=[], language="go")
        assert graph.has_edge("server.go::Handle", "server.go::internal")

    def test_package_qualified_call_resolves_via_dot_split(self, tmp_path):
        (tmp_path / "main.go").write_text(
            "func caller() {\n"
            "    pkg.Func()\n"
            "}\n"
        )
        (tmp_path / "pkg.go").write_text(
            "func Func() {\n"
            "}\n"
        )
        graph = callgraph.build_call_graph(tmp_path, included=["**/*.go"], excluded=[], language="go")
        assert graph.has_edge("main.go::caller", "pkg.go::Func")


class TestBuildMultiRepoCallGraph:
    def test_merges_nodes_and_edges_across_repos(self, tmp_path):
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        (repo_a / "lib.rs").write_text(
            "fn a_helper() -> i32 {\n    1\n}\n\nfn a_caller() -> i32 {\n    a_helper()\n}\n"
        )
        (repo_b / "lib.rs").write_text(
            "fn b_helper() -> i32 {\n    2\n}\n\nfn b_caller() -> i32 {\n    b_helper()\n}\n"
        )
        roots = [
            ("repo-a", repo_a, ["**/*.rs"], []),
            ("repo-b", repo_b, ["**/*.rs"], []),
        ]
        graph = callgraph.build_multi_repo_call_graph(roots, language="rust")

        assert "repo-a/lib.rs::a_helper" in graph.nodes
        assert "repo-b/lib.rs::b_helper" in graph.nodes
        assert graph.has_edge("repo-a/lib.rs::a_caller", "repo-a/lib.rs::a_helper")
        assert graph.has_edge("repo-b/lib.rs::b_caller", "repo-b/lib.rs::b_helper")

    def test_same_relative_path_in_two_repos_does_not_collide(self, tmp_path):
        """Both repos have a `lib.rs` defining a `helper` -- the repo_tag
        prefix must keep them as two distinct nodes, not one shared node
        that silently merges unrelated functions from different repos."""
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        (repo_a / "lib.rs").write_text("fn helper() -> i32 {\n    1\n}\n")
        (repo_b / "lib.rs").write_text("fn helper() -> i32 {\n    2\n}\n")
        roots = [
            ("repo-a", repo_a, ["**/*.rs"], []),
            ("repo-b", repo_b, ["**/*.rs"], []),
        ]
        graph = callgraph.build_multi_repo_call_graph(roots, language="rust")

        assert graph.number_of_nodes() == 2
        assert "repo-a/lib.rs::helper" in graph.nodes
        assert "repo-b/lib.rs::helper" in graph.nodes

    def test_call_does_not_resolve_across_repo_boundary(self, tmp_path):
        """A call in repo-a matching a same-named function defined only in
        repo-b must stay unresolved, not silently cross the repo
        boundary -- each repo's name_index is built and discarded before
        the next repo starts (see build_multi_repo_call_graph's docstring)."""
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        (repo_a / "lib.rs").write_text(
            "fn caller() -> i32 {\n    only_in_repo_b()\n}\n"
        )
        (repo_b / "lib.rs").write_text(
            "fn only_in_repo_b() -> i32 {\n    1\n}\n"
        )
        roots = [
            ("repo-a", repo_a, ["**/*.rs"], []),
            ("repo-b", repo_b, ["**/*.rs"], []),
        ]
        graph = callgraph.build_multi_repo_call_graph(roots, language="rust")

        assert not any(graph.successors("repo-a/lib.rs::caller"))
        assert graph.graph["unresolved_calls"] == 1

    def test_stats_are_summed_across_repos(self, tmp_path):
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        (repo_a / "lib.rs").write_text("fn caller() -> i32 {\n    unknown_a()\n}\n")
        (repo_b / "lib.rs").write_text("fn caller() -> i32 {\n    unknown_b()\n}\n")
        roots = [
            ("repo-a", repo_a, ["**/*.rs"], []),
            ("repo-b", repo_b, ["**/*.rs"], []),
        ]
        graph = callgraph.build_multi_repo_call_graph(roots, language="rust")

        assert graph.graph["total_calls"] == 2
        assert graph.graph["unresolved_calls"] == 2


class TestFormatLookupError:
    def test_with_candidates_lists_them(self):
        out = callgraph.format_lookup_error({"error": "'foo' not found or ambiguous", "candidates": ["a.py::foo", "b.py::foo"]})
        assert "a.py::foo" in out
        assert "b.py::foo" in out

    def test_without_candidates_suggests_qualified_name_syntax(self):
        out = callgraph.format_lookup_error({"error": "'foo' not found or ambiguous", "candidates": []})
        assert "qualified name" in out


class TestComputeFingerprint:
    def test_changes_when_a_file_is_modified(self, tmp_path):
        (tmp_path / "a.go").write_text("func A() {}\n")
        roots = [("repo", tmp_path, ["**/*.go"], [])]

        before = callgraph.compute_fingerprint(roots)
        (tmp_path / "a.go").write_text("func A() { doWork() }\n")
        after = callgraph.compute_fingerprint(roots)

        assert before != after

    def test_changes_when_a_file_is_added(self, tmp_path):
        (tmp_path / "a.go").write_text("func A() {}\n")
        roots = [("repo", tmp_path, ["**/*.go"], [])]

        before = callgraph.compute_fingerprint(roots)
        (tmp_path / "b.go").write_text("func B() {}\n")
        after = callgraph.compute_fingerprint(roots)

        assert before != after

    def test_stable_across_repeated_calls_with_no_changes(self, tmp_path):
        (tmp_path / "a.go").write_text("func A() {}\n")
        roots = [("repo", tmp_path, ["**/*.go"], [])]

        assert callgraph.compute_fingerprint(roots) == callgraph.compute_fingerprint(roots)

    def test_missing_root_does_not_raise(self, tmp_path):
        roots = [("repo", tmp_path / "does-not-exist", ["**/*.go"], [])]
        assert callgraph.compute_fingerprint(roots) == "0:0.000000"


class _FakeCacheCursor:
    def __init__(self, store: dict, capture: list):
        self._store = store
        self._capture = capture
        self._last_result = None

    def execute(self, sql, params=None):
        self._capture.append((" ".join(sql.split()), params))
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT fingerprint, graph_data"):
            self._last_result = self._store.get(params[0])
        elif normalized.startswith("INSERT INTO cocoindex.call_graph_cache"):
            cache_key, fingerprint, graph_data = params
            self._store[cache_key] = (fingerprint, graph_data)

    def fetchone(self):
        return self._last_result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeCacheConn:
    def __init__(self, store: dict, capture: list):
        self._store = store
        self._capture = capture

    def cursor(self):
        return _FakeCacheCursor(self._store, self._capture)

    def commit(self):
        pass

    def close(self):
        pass


class TestBuildMultiRepoCallGraphWithStatsCached:
    """Mocks psycopg2 entirely (a real round-trip against Postgres is
    covered separately in tests/integration/) -- these pin down the pure
    Python hit/miss/fallback branching and the fingerprint-only (no TTL)
    invalidation contract."""

    def _write_repo(self, tmp_path):
        (tmp_path / "a.go").write_text("func helper() int {\n    return 1\n}\n\nfunc caller() int {\n    return helper()\n}\n")
        return [("repo", tmp_path, ["**/*.go"], [])]

    def _patch_psycopg2(self, monkeypatch, store: dict) -> list:
        import psycopg2
        capture: list = []
        monkeypatch.setattr(psycopg2, "connect", lambda url: _FakeCacheConn(store, capture))
        monkeypatch.setattr(callgraph, "_CACHE_TABLE_READY", False)
        return capture

    def test_first_call_is_a_miss_and_populates_the_cache(self, tmp_path, monkeypatch):
        roots = self._write_repo(tmp_path)
        store: dict = {}
        self._patch_psycopg2(monkeypatch, store)

        graph = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "test-key", "postgresql://fake")

        assert graph.has_edge("repo/a.go::caller", "repo/a.go::helper")
        assert "test-key" in store

    def test_second_call_with_no_changes_is_a_cache_hit_not_a_rebuild(self, tmp_path, monkeypatch):
        roots = self._write_repo(tmp_path)
        store: dict = {}
        self._patch_psycopg2(monkeypatch, store)
        callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "test-key", "postgresql://fake")

        rebuilt = []
        monkeypatch.setattr(
            callgraph, "build_multi_repo_call_graph_with_stats",
            lambda *a, **k: rebuilt.append(1) or nx.DiGraph(),
        )
        graph = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "test-key", "postgresql://fake")

        assert rebuilt == []
        assert graph.has_edge("repo/a.go::caller", "repo/a.go::helper")

    def test_file_change_invalidates_the_cache_and_triggers_a_rebuild(self, tmp_path, monkeypatch):
        roots = self._write_repo(tmp_path)
        store: dict = {}
        self._patch_psycopg2(monkeypatch, store)
        callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "test-key", "postgresql://fake")

        (tmp_path / "a.go").write_text(
            "func helper() int {\n    return 2\n}\n\nfunc caller() int {\n    return helper()\n}\n\nfunc newFunc() {}\n"
        )
        graph = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "test-key", "postgresql://fake")

        assert "repo/a.go::newFunc" in graph.nodes

    def test_different_cache_keys_do_not_collide(self, tmp_path, monkeypatch):
        roots = self._write_repo(tmp_path)
        store: dict = {}
        self._patch_psycopg2(monkeypatch, store)

        callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "key-a", "postgresql://fake")
        callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "key-b", "postgresql://fake")

        assert set(store.keys()) == {"key-a", "key-b"}

    def test_connect_failure_falls_back_to_uncached_rebuild(self, tmp_path, monkeypatch):
        import psycopg2
        roots = self._write_repo(tmp_path)

        def _raise(url):
            raise psycopg2.OperationalError("connection refused")

        monkeypatch.setattr(psycopg2, "connect", _raise)

        graph = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "test-key", "postgresql://fake")

        assert graph.has_edge("repo/a.go::caller", "repo/a.go::helper")

    def test_corrupt_cache_row_falls_back_to_rebuild_rather_than_raising(self, tmp_path, monkeypatch):
        roots = self._write_repo(tmp_path)
        fingerprint = callgraph.compute_fingerprint(roots)
        store = {"test-key": (fingerprint, b"not-valid-pickle-data")}
        self._patch_psycopg2(monkeypatch, store)

        graph = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "test-key", "postgresql://fake")

        assert graph.has_edge("repo/a.go::caller", "repo/a.go::helper")

    def test_write_failure_after_a_real_rebuild_does_not_raise(self, tmp_path, monkeypatch):
        import psycopg2
        roots = self._write_repo(tmp_path)
        store: dict = {}
        calls = {"n": 0}

        def _connect(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeCacheConn(store, [])
            raise psycopg2.OperationalError("connection lost")

        monkeypatch.setattr(psycopg2, "connect", _connect)
        monkeypatch.setattr(callgraph, "_CACHE_TABLE_READY", False)

        graph = callgraph.build_multi_repo_call_graph_with_stats_cached(roots, "go", "test-key", "postgresql://fake")

        assert graph.has_edge("repo/a.go::caller", "repo/a.go::helper")
        assert "test-key" not in store
