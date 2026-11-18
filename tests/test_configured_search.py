from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import inspect
import sys
import threading
import time
import types

import pytest

from engram.search import engram as search
from engram.search import configured
from engram.project_config import ProjectSource


def test_query_embedding_model_is_initialized_and_used_serially(monkeypatch):
    """Concurrent callers share one model and never overlap inference."""
    model_creations = []
    active_encodes = 0
    max_active_encodes = 0
    state_lock = threading.Lock()

    class FakeVector:
        def tolist(self):
            return [0.1, 0.2]

    class FakeModel:
        def __init__(self, model_name, *, device):
            model_creations.append((model_name, device))
            time.sleep(0.01)

        def encode(self, _query, *, normalize_embeddings):
            nonlocal active_encodes, max_active_encodes
            assert normalize_embeddings is True
            with state_lock:
                active_encodes += 1
                max_active_encodes = max(max_active_encodes, active_encodes)
            time.sleep(0.01)
            with state_lock:
                active_encodes -= 1
            return FakeVector()

    sentence_transformers = types.ModuleType("sentence_transformers")
    sentence_transformers.SentenceTransformer = FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", sentence_transformers)
    monkeypatch.setattr(search, "_model", None)
    monkeypatch.setattr(search, "EMBEDDING_MODEL", "test-model")
    monkeypatch.setattr(search, "EMBEDDING_DEVICE", "cpu")

    with ThreadPoolExecutor(max_workers=4) as executor:
        vectors = list(executor.map(search._embed_query, ["query"] * 8))

    assert model_creations == [("test-model", "cpu")]
    assert max_active_encodes == 1
    assert vectors == [[0.1, 0.2]] * 8


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


def test_configure_project_no_longer_configures_name_based_call_graph(monkeypatch, tmp_path):
    auto_root = tmp_path / "docs"
    auto_root.mkdir()

    names = (
        "CODE_TABLE",
        "EMBEDDING_MODEL",
        "PG_URL",
        "SEARCH_PROJECT",
        "_PATTERN_SEARCH_ROOTS",
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
        )

        assert not hasattr(search, "_CALL_GRAPH_ROOTS")
        assert not hasattr(search, "CALL_GRAPH_ROOT")
    finally:
        for name, value in previous.items():
            setattr(search, name, value)


def test_retired_call_graph_tools_are_fully_removed():
    # issue #199: the name-based spike and its deprecation shims are gone;
    # graph queries route exclusively through the zvec-grep MCP tools.
    assert not hasattr(search, "call_graph_deprecation_error")
    assert not hasattr(search, "DEPRECATED_CALL_GRAPH_TOOLS")
    source = inspect.getsource(search)
    assert "call_graph_blast_radius" not in source
    assert "call_graph_shortest_path" not in source
    assert "call_graph_get_cluster" not in source


def test_pattern_search_language_default_follows_project_configuration(monkeypatch):
    # issue #199 follow-up (CodeRabbit): a Go-only project's pattern search
    # without an explicit language must default to Go, not Python.
    import engram.search.engram as search_mod

    monkeypatch.setattr(
        search_mod, "_select_pattern_roots",
        lambda repo, branch: [("go-root", "/tmp/go-root", [], [], "go", None)],
    )
    assert search_mod._pattern_search_language_default("go-root", None) == "go"
    assert search_mod._pattern_search_language_default(None, None) == "go"

    # mixed or auto-only roots fall back to the historical Python default
    monkeypatch.setattr(
        search_mod, "_select_pattern_roots",
        lambda repo, branch: [
            ("go-root", "/tmp/go-root", [], [], "go", None),
            ("py-root", "/tmp/py-root", [], [], "python", None),
        ],
    )
    assert search_mod._pattern_search_language_default(None, None) == "python"
    monkeypatch.setattr(
        search_mod, "_select_pattern_roots",
        lambda repo, branch: [("auto-root", "/tmp/auto-root", [], [], "auto", None)],
    )
    assert search_mod._pattern_search_language_default(None, None) == "python"


def test_pattern_search_invalid_repo_without_language_returns_friendly_error():
    # CodeRabbit follow-up: the language-default derivation selects roots and
    # can raise ValueError for a malformed repo (unsafe-scope validation), so
    # it must run inside the tool's try block to keep the friendly error.
    import inspect

    import engram.search.engram as search_mod

    with pytest.raises(ValueError):
        search_mod._pattern_search_language_default("../bad", None)

    source = inspect.getsource(search_mod._run_mcp_server)
    tool_body = source[source.index("def engram_code_pattern_search"):]
    try_pos = tool_body.index("try:")
    default_pos = tool_body.index("_pattern_search_language_default(repo, branch)")
    assert try_pos < default_pos, "language default must be computed inside the try block"


# ---------------------------------------------------------------------------
# Fix 7 + Fix 8 (issue #199 follow-up): language canonicalization and the
# unified CLI/MCP default. Matrix from the pr200-alias-spike preflight.
# ---------------------------------------------------------------------------

_MATRIX_FILE_CONTENTS = {
    "t.sh": "#!/bin/bash\necho hi\n",
    "t.go": "package main\n",
    "t.py": "x = 1\n",
    "t.ts": "const a = 1;\n",
    "t.tsx": "export const A = () => null;\n",
    "tjsx.tsx": 'export const B = () => <div className="x">hi</div>;\n',
    "t.tf": 'resource "x" "y" {}\n',
    "t.md": "# hi\n",
}


def _matrix_root(tmp_path, name, files, language):
    root = tmp_path / name
    root.mkdir()
    for filename in files:
        (root / filename).write_text(_MATRIX_FILE_CONTENTS[filename])
    return (name, root, ["**/*"], [], language, None)


def _run_matrix_case(monkeypatch, tmp_path, roots, language=None):
    """Run a real pattern_search_code over the given roots. ``language=None``
    emulates the MCP wrapper: derive the default first, then search."""
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", None)
    effective = (
        language
        if language is not None
        else search._pattern_search_language_default(None, None)
    )
    results = search.pattern_search_code("\\NAME", effective, limit=50)
    return {r["filepath"] for r in results}


def test_pattern_language_matrix_shell_tag_default_and_explicit(monkeypatch, tmp_path):
    # cases 1-3: shell/sh tags (default and explicit) match a real .sh file
    for tag in ("shell", "sh"):
        roots = [_matrix_root(tmp_path, f"{tag}-default", ["t.sh"], tag)]
        assert _run_matrix_case(monkeypatch, tmp_path, roots) == {f"{tag}-default/t.sh"}
        assert _run_matrix_case(monkeypatch, tmp_path, roots, language=tag) == {
            f"{tag}-default/t.sh"
        }


def test_pattern_language_matrix_golang_tag_alias(monkeypatch, tmp_path):
    # case 4: explicit golang -> go
    roots = [_matrix_root(tmp_path, "golang-root", ["t.go"], "golang")]
    assert _run_matrix_case(monkeypatch, tmp_path, roots, language="golang") == {
        "golang-root/t.go"
    }


def test_pattern_language_matrix_typescript_and_tsx_detection_pair(monkeypatch, tmp_path):
    # cases 5-7: .ts and .tsx under a typescript tag; .tsx under a tsx tag
    roots = [_matrix_root(tmp_path, "ts-root", ["t.ts", "t.tsx"], "typescript")]
    assert _run_matrix_case(monkeypatch, tmp_path, roots) == {
        "ts-root/t.ts",
        "ts-root/t.tsx",
    }
    roots = [_matrix_root(tmp_path, "tsx-root", ["t.tsx"], "tsx")]
    assert _run_matrix_case(monkeypatch, tmp_path, roots) == {"tsx-root/t.tsx"}


def test_pattern_language_matrix_typescript_uses_tsx_grammar_for_jsx(monkeypatch, tmp_path):
    # CodeRabbit follow-up: .tsx files containing JSX must still match when
    # the search language is `typescript`. The TypeScript grammar cannot
    # parse JSX (tree-sitter error recovery drops the JSX nodes), so the
    # .tsx fixture must be matched with the tsx grammar; before the fix the
    # JSX file was silently dropped. The pattern below matches only when
    # parsed with the tsx grammar (verified: typescript grammar -> no match).
    roots = [_matrix_root(tmp_path, "ts-jsx-root", ["t.ts", "t.tsx", "tjsx.tsx"], "typescript")]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", None)
    results = search.pattern_search_code(
        '<div className="x">\\NAME</div>', "typescript", limit=50
    )
    assert {r["filepath"] for r in results} == {"ts-jsx-root/tjsx.tsx"}
    # explicit tsx language matches the same fixture via its own grammar
    roots = [_matrix_root(tmp_path, "tsx-jsx-root", ["tjsx.tsx"], "tsx")]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    results = search.pattern_search_code(
        '<div className="x">\\NAME</div>', "tsx", limit=50
    )
    assert {r["filepath"] for r in results} == {"tsx-jsx-root/tjsx.tsx"}


def test_pattern_language_matrix_markdown_tag_fails_safe_to_python(monkeypatch, tmp_path):
    # case 8: a markdown-tagged root yields the python default (fail-safe);
    # the root itself is then skipped by the root-tag filter (its canonical
    # language is None, never equal to python), so nothing matches -- in
    # particular t.md can never match.
    roots = [_matrix_root(tmp_path, "md-root", ["t.md", "t.py"], "markdown")]
    assert search._pattern_search_language_default(None, None) == "python"
    assert _run_matrix_case(monkeypatch, tmp_path, roots) == set()


def test_pattern_language_matrix_explicit_unsupported_language_raises(monkeypatch, tmp_path):
    # case 9: explicit markdown raises loudly (only the default fails safe)
    roots = [_matrix_root(tmp_path, "md-root", ["t.md"], "markdown")]
    monkeypatch.setattr(search, "_PATTERN_SEARCH_ROOTS", roots)
    monkeypatch.setattr(search, "_DEFAULT_BRANCH", None)
    with pytest.raises(ValueError, match="not supported for language"):
        search.pattern_search_code("\\NAME", "markdown")


def test_pattern_language_matrix_mixed_project_defaults_to_python(monkeypatch, tmp_path):
    # case 10: mixed go+python -> python (retained), only the .py file matches
    roots = [
        _matrix_root(tmp_path, "go-root", ["t.go"], "go"),
        _matrix_root(tmp_path, "py-root", ["t.py"], "python"),
    ]
    assert search._pattern_search_language_default(None, None) == "python"
    assert _run_matrix_case(monkeypatch, tmp_path, roots) == {"py-root/t.py"}


def test_pattern_language_matrix_explicit_language_skips_other_tagged_root(monkeypatch, tmp_path):
    # case 11 (negative control): go-tagged root skipped under python
    roots = [_matrix_root(tmp_path, "go-root", ["t.go"], "go")]
    assert _run_matrix_case(monkeypatch, tmp_path, roots, language="python") == set()


def test_pattern_language_matrix_detection_filter_excludes_other_language_files(monkeypatch, tmp_path):
    # case 12: python root with .py and .sh files -> only .py matches
    roots = [_matrix_root(tmp_path, "py-root", ["t.py", "t.sh"], "python")]
    assert _run_matrix_case(monkeypatch, tmp_path, roots) == {"py-root/t.py"}


def test_pattern_language_matrix_auto_tag_passes_through(monkeypatch, tmp_path):
    # case 13: auto root + explicit python matches
    roots = [_matrix_root(tmp_path, "auto-root", ["t.py"], "auto")]
    assert _run_matrix_case(monkeypatch, tmp_path, roots, language="python") == {
        "auto-root/t.py"
    }


def test_pattern_language_matrix_terraform_two_hop_alias(monkeypatch, tmp_path):
    # case 14: terraform tag -> hcl canonical; .tf detects as hcl
    roots = [_matrix_root(tmp_path, "tf-root", ["t.tf"], "terraform")]
    assert _run_matrix_case(monkeypatch, tmp_path, roots) == {"tf-root/t.tf"}


def test_pattern_language_alias_targets_are_codepattern_accepted():
    """Pin the alias map against cocoindex's live accepted set: every alias
    RHS and detection-equivalent key must still construct a CodePattern.
    Catches cocoindex upgrades silently shrinking the vocabulary (which
    would otherwise degrade to the python default via the probe cache)."""
    from cocoindex.ops.code import CodePattern

    for canonical in {*search._PATTERN_LANGUAGE_ALIASES.values(), *search._DETECTION_EQUIVALENTS}:
        CodePattern(search._PATTERN_PROBE_PLACEHOLDER, canonical)  # must not raise


def test_pattern_language_aliases_map_is_complete_and_directional():
    # every alias key is lowercase and distinct from its canonical value;
    # every canonical value is itself in the detection vocabulary space
    # (no alias maps to another alias).
    for alias, canonical in search._PATTERN_LANGUAGE_ALIASES.items():
        assert alias == alias.lower()
        assert alias != canonical
        assert canonical not in search._PATTERN_LANGUAGE_ALIASES


def test_cli_pattern_default_routes_through_unified_derivation(monkeypatch):
    # Fix 8: the CLI's --pattern default comes from the same helper as the
    # MCP surface; a mixed go+python project yields python on both.
    captured = {}
    monkeypatch.setattr(
        configured,
        "load_adapter_config",
        lambda project, config: SimpleNamespace(project="mixed"),
    )
    monkeypatch.setattr(configured, "configure", lambda config: None)
    monkeypatch.setattr(
        search,
        "_select_pattern_roots",
        lambda repo, branch: [
            ("go-root", "/tmp/go-root", [], [], "go", None),
            ("py-root", "/tmp/py-root", [], [], "python", None),
        ],
    )

    def fake_cli(pattern, language, **kwargs):
        captured.update(pattern=pattern, language=language)

    monkeypatch.setattr(configured.engine, "_run_cli_pattern_query", fake_cli)
    monkeypatch.setattr(
        sys, "argv", ["configured", "--project", "mixed", "--pattern", "\\NAME"]
    )
    configured.main()
    assert captured == {"pattern": "\\NAME", "language": "python"}


def test_cli_malformed_repo_language_default_yields_cli_error_not_traceback(monkeypatch):
    # Fix 8 guard: the unified helper can raise ValueError for a malformed
    # repo (the crash edd4924 fixed for MCP); the CLI must keep its error
    # style instead of an unhandled traceback.
    monkeypatch.setattr(
        configured,
        "load_adapter_config",
        lambda project, config: SimpleNamespace(project="broken"),
    )
    monkeypatch.setattr(configured, "configure", lambda config: None)

    def boom(repo, branch):
        raise ValueError("unsafe scope")

    monkeypatch.setattr(configured.engine, "_pattern_search_language_default", boom)
    monkeypatch.setattr(
        sys, "argv", ["configured", "--project", "broken", "--pattern", "\\NAME"]
    )
    with pytest.raises(SystemExit) as excinfo:
        configured.main()
    assert "unsafe scope" in str(excinfo.value)
