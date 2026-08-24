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
