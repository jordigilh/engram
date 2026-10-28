from __future__ import annotations

from types import SimpleNamespace

import pytest

from engram.search import engram as search
from engram.search import configured
from engram.project_config import ProjectSource


def test_branch_where_uses_release_source_tag():
    where, params = search._branch_where("repo", "v1.5")

    assert where.startswith(" AND ")
    assert params == ["repo@release-v1.5/%"]


def test_pattern_root_selection_is_repository_and_branch_aware(monkeypatch, tmp_path):
    roots = [
        ("repo", tmp_path / "main", ["**/*.py"], [], "python", None),
        ("repo@release-v1.5", tmp_path / "release", ["**/*.py"], [], "python", "v1.5"),
        ("other", tmp_path / "other", ["**/*.py"], [], "python", None),
    ]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", None)

    assert [root[0] for root in search._select_pattern_roots("repo", "v1.5")] == [
        "repo@release-v1.5"
    ]
    assert [root[0] for root in search._select_pattern_roots("repo", "main")] == ["repo"]


def test_pattern_root_selection_accepts_public_alias_for_legacy_source_tag(monkeypatch, tmp_path):
    roots = [
        ("example-repo", tmp_path / "example", ["**/*.go"], [], "go", None),
        ("example@release-v1.5", tmp_path / "example-release-v1.5", ["**/*.go"], [], "go", "v1.5"),
    ]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_SCOPE_ALIASES", {
        "example-repo": frozenset({"example-repo", "example"}),
        "example@release-v1.5": frozenset({"example@release-v1.5", "example"}),
    })
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", "main")

    assert [root[0] for root in search._select_pattern_roots("example", "main")] == [
        "example-repo"
    ]
    assert [root[0] for root in search._select_pattern_roots("example", "v1.5")] == [
        "example@release-v1.5"
    ]


def test_unknown_scope_error_lists_valid_aliases(monkeypatch, tmp_path):
    roots = [("example-repo", tmp_path / "example", ["**/*.go"], [], "go", None)]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", "main")

    error = search._scope_error("missing", "main", roots)

    assert "missing" in str(error)
    assert "valid scopes: example, example-repo" in str(error)


def test_unknown_scope_error_identifies_requested_branch(monkeypatch, tmp_path):
    roots = [("example", tmp_path / "example", ["**/*.go"], [], "go", None)]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", "main")

    error = search._scope_error("example", "v9.9", roots)

    assert "branch 'v9.9'" in str(error)


def test_semantic_scope_expands_public_alias_and_legacy_index_prefix(monkeypatch, tmp_path):
    roots = [("example-repo", tmp_path / "example", ["**/*.go"], [], "go", None)]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", "main")
    monkeypatch.setattr(
        search,
        "_SCOPE_ALIASES",
        {"example-repo": frozenset({"example-repo", "example"})},
    )
    monkeypatch.setattr(
        search,
        "_SCOPE_INDEX_TAGS",
        {"example-repo": frozenset({"example-repo", "example"})},
    )

    where, params = search._branch_where("example", "main")

    assert "filepath LIKE %s" in where
    assert "example/%" in params
    assert "example-repo/%" in params
    assert "%@%" in params


def test_branch_where_handles_release_and_unscoped_branch_filters(monkeypatch, tmp_path):
    roots = [
        ("example@release-v1.5", tmp_path / "release", ["**/*.go"], [], "go", "v1.5"),
    ]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", None)
    monkeypatch.setattr(search, "_SCOPE_ALIASES", {
        "example@release-v1.5": frozenset({"example", "example@release-v1.5"}),
    })
    monkeypatch.setattr(search, "_SCOPE_INDEX_TAGS", {
        "example@release-v1.5": frozenset({"example@release-v1.5"}),
    })

    release_where, release_params = search._branch_where("example", "v1.5")
    unscoped_where, unscoped_params = search._branch_where(None, "v1.5")

    assert "filepath LIKE" in release_where
    assert "example@release-v1.5/%" in release_params
    assert unscoped_where.endswith("filepath LIKE %s")
    assert unscoped_params == ["%@release-v1.5/%"]


def test_semantic_scope_does_not_expand_into_another_source_tag(monkeypatch, tmp_path):
    roots = [
        ("example-repo", tmp_path / "first", ["**/*.go"], [], "go", None),
        ("example", tmp_path / "second", ["**/*.go"], [], "go", None),
    ]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", "main")
    monkeypatch.setattr(search, "_SCOPE_ALIASES", {
        "example-repo": frozenset({"example-repo", "example"}),
        "example": frozenset({"example"}),
    })
    monkeypatch.setattr(search, "_SCOPE_INDEX_TAGS", {
        "example-repo": frozenset({"example-repo"}),
        "example": frozenset({"example"}),
    })

    _where, params = search._branch_where("example-repo", "main")

    assert "example-repo/%" in params
    assert "example/%" not in params


def test_semantic_scope_rejects_ambiguous_branch_qualified_alias(monkeypatch, tmp_path):
    roots = [
        ("alpha@release-v1.5", tmp_path / "alpha", ["**/*.go"], [], "go", "v1.5"),
        ("example@release-v1.5", tmp_path / "example", ["**/*.go"], [], "go", "v1.5"),
    ]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", "main")
    monkeypatch.setattr(search, "_SCOPE_ALIASES", {
        "alpha@release-v1.5": frozenset({"alpha@release-v1.5", "example@release-v1.5"}),
        "example@release-v1.5": frozenset({"example@release-v1.5"}),
    })
    monkeypatch.setattr(search, "_SCOPE_INDEX_TAGS", {
        "alpha@release-v1.5": frozenset({"alpha@release-v1.5"}),
        "example@release-v1.5": frozenset({"example@release-v1.5"}),
    })

    with pytest.raises(ValueError, match="matching sources"):
        search._branch_where("example@release-v1.5", "v1.5")


def test_format_results_reports_scope_and_truncation():
    result = search._format_results(
        "HandleApprove",
        [{"filepath": "example-repo/pkg/tools.go", "score": 1.0, "code": "x" * 600}],
        repo="example",
        branch="main",
    )

    assert "scope: repo=example branch=main" in result
    assert "truncated=true" in result


def test_search_and_pattern_paths_fail_closed_for_unknown_scope(monkeypatch, tmp_path):
    roots = [("repo", tmp_path / "repo", ["**/*.py"], [], "python", None)]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", "main")

    with pytest.raises(ValueError, match="unknown code repository scope"):
        search.search_code("anything", repo="missing")
    with pytest.raises(ValueError, match="unknown code repository scope"):
        search.pattern_search_code("def \\NAME", "python", repo="missing")


def test_configure_passes_source_scope_aliases_to_the_search_engine(monkeypatch, tmp_path):
    captured = {}

    def fake_configure_project(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(configured.engine, "configure_project", fake_configure_project)
    source = ProjectSource(
        tag="example-repo",
        root=tmp_path / "example",
        docs_include=(),
        docs_exclude=(),
        code_include=("**/*.go",),
        code_exclude=(),
        language="go",
    )
    config = SimpleNamespace(
        project="example",
        code_table="code_embeddings",
        pg_dsn="postgresql://localhost/db",
        embedding_model="test-model",
        code_sources=(source,),
        default_branch="main",
    )

    configured.configure(config)

    assert "example" in captured["scope_aliases"]["example-repo"]
    assert captured["scope_index_tags"]["example-repo"] == ("example-repo",)


def test_call_graph_merges_explicitly_configured_languages(monkeypatch, tmp_path):
    python_root = tmp_path / "python"
    go_root = tmp_path / "go"
    python_root.mkdir()
    go_root.mkdir()
    (python_root / "mod.py").write_text(
        "def helper():\n    return 1\n\ndef caller():\n    return helper()\n"
    )
    (go_root / "mod.go").write_text(
        "package main\n\nfunc helper() int { return 1 }\n\nfunc caller() int { return helper() }\n"
    )
    roots = [
        ("python-repo", python_root, ["**/*.py"], [], "python", None),
        ("go-repo", go_root, ["**/*.go"], [], "go", None),
    ]
    monkeypatch.setattr(search, "_CALL_GRAPH_ROOTS", roots)
    monkeypatch.setattr(search, "CALL_GRAPH_LANGUAGE", "auto")
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", None)

    graph = search._build_graph_with_timing()

    assert "python-repo/mod.py::caller" in graph
    assert "go-repo/mod.go::caller" in graph
    assert graph.has_edge("python-repo/mod.py::caller", "python-repo/mod.py::helper")
    assert graph.has_edge("go-repo/mod.go::caller", "go-repo/mod.go::helper")


def test_configure_project_can_disable_call_graph_for_auto_only_roots(monkeypatch, tmp_path):
    auto_root = tmp_path / "docs"
    auto_root.mkdir()

    names = (
        "CODE_TABLE",
        "EMBEDDING_MODEL",
        "PG_URL",
        "SEARCH_PROJECT",
        "CALL_GRAPH_ROOT",
        "CALL_GRAPH_LANGUAGE",
        "_PATTERN_SEARCH_ROOTS",
        "_CALL_GRAPH_ROOTS",
        "_DEFAULT_BRANCH",
        "_model",
    )
    previous = {name: getattr(search, name) for name in names}
    try:
        search.configure_project(
            project="docs-only",
            pg_url="postgresql://localhost/db",
            code_table="docs_code_embeddings",
            embedding_model="test-model",
            pattern_roots=[("docs", auto_root, ["**/*.yaml"], [], "auto", None)],
            call_graph_roots=[],
        )

        assert search._CALL_GRAPH_ROOTS == []
    finally:
        for name, value in previous.items():
            setattr(search, name, value)


def test_call_graph_branch_scope_excludes_other_release_lines(monkeypatch, tmp_path):
    main_root = tmp_path / "main"
    release_root = tmp_path / "release"
    main_root.mkdir()
    release_root.mkdir()
    (main_root / "mod.go").write_text(
        "package main\n\nfunc helper() int { return 1 }\n\nfunc mainOnly() int { return helper() }\n"
    )
    (release_root / "mod.go").write_text(
        "package main\n\nfunc helper() int { return 1 }\n\nfunc releaseOnly() int { return helper() }\n"
    )
    monkeypatch.setattr(search, "_CALL_GRAPH_ROOTS", [
        ("repo", main_root, ["**/*.go"], [], "go", None),
        ("repo@release-v1.5", release_root, ["**/*.go"], [], "go", "v1.5"),
    ])
    monkeypatch.setattr(search, "CALL_GRAPH_LANGUAGE", "go")
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", None)

    result = search.call_graph_blast_radius("repo@release-v1.5/mod.go::helper", branch="v1.5")

    assert result["callers_by_depth"] == [["repo@release-v1.5/mod.go::releaseOnly"]]
