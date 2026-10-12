from __future__ import annotations

from engram.search import engram as search


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
