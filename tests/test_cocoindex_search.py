"""Tests for cocoindex-search.py's multi-branch (2026-08-10) release-line
scoping: _detect_current_release_line(), _resolve_release_line(),
_branch_where(), _select_pattern_roots(), and their wiring into
search_code()/pattern_search_code(). See docs/FINDINGS.md and
tests/test_cocoindex_flows.py::TestReleaseLineWiring for the ingestion-side
counterpart (code_main tagging rows with repo_tag="{repo}@release-{line}").
"""
from __future__ import annotations

import subprocess


class TestDetectCurrentReleaseLine:
    def test_returns_none_when_live_clone_dir_unset(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR", None)
        assert cocoindex_search._detect_current_release_line() is None

    def test_returns_line_for_recognized_release_branch(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR", "/fake/clone")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="release/v1.5\n", stderr="")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() == "v1.5"

    def test_returns_none_for_main_branch(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR", "/fake/clone")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() is None

    def test_returns_none_for_feature_branch(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR", "/fake/clone")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="fix/some-bug\n", stderr="")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() is None

    def test_returns_none_for_release_line_not_in_configured_set(self, cocoindex_search, monkeypatch):
        """release/v1.4 (say) parses as a release branch shape but isn't one
        of the two proactively-mirrored lines -- must not match."""
        monkeypatch.setattr(cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR", "/fake/clone")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="release/v1.4\n", stderr="")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() is None

    def test_returns_none_when_git_command_fails(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR", "/fake/clone")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a git repo")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() is None

    def test_returns_none_when_git_binary_missing(self, cocoindex_search, monkeypatch):
        """No dir-name suffix on /fake/clone either, so this still falls
        through to None."""
        monkeypatch.setattr(cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR", "/fake/clone")

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() is None

    def test_dirname_fallback_used_when_branch_is_a_feature_branch(self, cocoindex_search, monkeypatch):
        """Real day-to-day convention: a dedicated per-release-line clone
        directory (e.g. kubernaut-v1.5) with a feature/fix branch checked
        out inside it -- not literally on release/v1.5. Confirmed live
        2026-08-10 against the actual kubernaut-v1.5 clone."""
        monkeypatch.setattr(
            cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR",
            "/Users/jgil/go/src/github.com/jordigilh/kubernaut-v1.5",
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="fix/2086-workflow-discovery-hang\n", stderr="")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() == "v1.5"

    def test_dirname_fallback_used_when_git_command_fails(self, cocoindex_search, monkeypatch):
        """Same dir-name convention still applies even if git itself can't
        report a branch (e.g. detached HEAD, git missing, not a repo)."""
        monkeypatch.setattr(
            cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR",
            "/Users/jgil/go/src/github.com/jordigilh/kubernaut-operator-v1.6",
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: not a git repository")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() == "v1.6"

    def test_dirname_without_version_suffix_returns_none(self, cocoindex_search, monkeypatch):
        """The plain "kubernaut" (main) clone has no -vX.Y suffix -- must
        not accidentally match anything."""
        monkeypatch.setattr(
            cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR",
            "/Users/jgil/go/src/github.com/jordigilh/kubernaut",
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="port/2071-2073-2075-2080-to-main\n", stderr="")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() is None

    def test_dirname_suffix_not_in_configured_lines_returns_none(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(
            cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR",
            "/Users/jgil/go/src/github.com/jordigilh/kubernaut-v1.4",
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() is None

    def test_literal_release_branch_wins_over_dirname_fallback(self, cocoindex_search, monkeypatch):
        """If someone actually is on release/v1.6 inside a v1.5-named
        directory (edge case), the literal branch check takes priority."""
        monkeypatch.setattr(
            cocoindex_search, "KUBERNAUT_LIVE_CLONE_DIR",
            "/Users/jgil/go/src/github.com/jordigilh/kubernaut-v1.5",
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="release/v1.6\n", stderr="")

        monkeypatch.setattr(cocoindex_search.subprocess, "run", fake_run)
        assert cocoindex_search._detect_current_release_line() == "v1.6"


class TestResolveReleaseLine:
    def test_none_branch_delegates_to_detection(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "_detect_current_release_line", lambda: "v1.6")
        assert cocoindex_search._resolve_release_line(None) == "v1.6"

    def test_explicit_branch_overrides_detection(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "_detect_current_release_line", lambda: "v1.6")
        assert cocoindex_search._resolve_release_line("v1.5") == "v1.5"

    def test_explicit_main_forces_none_even_if_detection_would_differ(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "_detect_current_release_line", lambda: "v1.5")
        assert cocoindex_search._resolve_release_line("main") is None

    def test_unrecognized_explicit_branch_falls_back_to_none(self, cocoindex_search):
        assert cocoindex_search._resolve_release_line("v9.9") is None


class TestBranchWhere:
    def test_no_repo_no_release_line_excludes_tagged_rows(self, cocoindex_search):
        where, params = cocoindex_search._branch_where(None, None)
        assert "NOT LIKE" in where
        assert params == ["%@%"]

    def test_repo_scoped_main_excludes_tagged_rows_and_scopes_repo(self, cocoindex_search):
        where, params = cocoindex_search._branch_where("kubernaut-operator", None)
        assert where.count("LIKE") == 2  # one plain LIKE (repo prefix), one NOT LIKE (exclude tagged)
        assert "kubernaut-operator/%" in params
        assert "%@%" in params

    def test_release_line_without_repo_scopes_to_any_repo_on_that_line(self, cocoindex_search):
        where, params = cocoindex_search._branch_where(None, "v1.5")
        assert params == ["%@release-v1.5/%"]

    def test_release_line_with_repo_scopes_to_exact_tag(self, cocoindex_search):
        where, params = cocoindex_search._branch_where("kubernaut", "v1.6")
        assert params == ["kubernaut@release-v1.6/%"]

    def test_where_fragment_is_syntactically_appendable(self, cocoindex_search):
        """Callers splice this directly after a WHERE/AND clause -- must
        start with " AND " (leading space) whenever it's non-empty."""
        where, _ = cocoindex_search._branch_where("kubernaut", "v1.5")
        assert where.startswith(" AND ")


class TestSelectPatternRoots:
    def test_no_scoping_returns_all_three_main_repos(self, cocoindex_search):
        tags = [r[0] for r in cocoindex_search._select_pattern_roots(None, None)]
        assert set(tags) == {"kubernaut", "kubernaut-operator", "kubernaut-console"}

    def test_release_line_without_repo_returns_all_three_tagged_repos(self, cocoindex_search):
        tags = [r[0] for r in cocoindex_search._select_pattern_roots(None, "v1.5")]
        assert set(tags) == {
            "kubernaut@release-v1.5",
            "kubernaut-operator@release-v1.5",
            "kubernaut-console@release-v1.5",
        }

    def test_repo_and_release_line_returns_single_exact_tag(self, cocoindex_search):
        roots = cocoindex_search._select_pattern_roots("kubernaut-operator", "v1.6")
        assert [r[0] for r in roots] == ["kubernaut-operator@release-v1.6"]

    def test_repo_without_release_line_returns_single_main_tag(self, cocoindex_search):
        roots = cocoindex_search._select_pattern_roots("kubernaut-console", None)
        assert [r[0] for r in roots] == ["kubernaut-console"]

    def test_unknown_repo_returns_nothing(self, cocoindex_search):
        assert cocoindex_search._select_pattern_roots("nonexistent-repo", None) == []
        assert cocoindex_search._select_pattern_roots("nonexistent-repo", "v1.5") == []


class _FakeCursor:
    def __init__(self, capture: list):
        self._capture = capture

    def execute(self, sql, params):
        self._capture.append((sql, params))

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, capture: list):
        self._capture = capture

    def cursor(self):
        return _FakeCursor(self._capture)

    def close(self):
        pass


class TestSearchCodeBranchWiring:
    """search_code() itself, with psycopg2/the embedding model faked out --
    only the SQL/params it builds are under test here, not real retrieval."""

    def _capture_queries(self, cocoindex_search, monkeypatch):
        capture: list = []
        monkeypatch.setattr(cocoindex_search, "_embed_query", lambda q: [0.0] * 4)
        # search_code() does `import psycopg2` locally (not a module-level
        # attribute of cocoindex_search), so patch the real module in
        # sys.modules that import resolves to, not cocoindex_search itself.
        import psycopg2

        monkeypatch.setattr(psycopg2, "connect", lambda url: _FakeConn(capture))
        return capture

    def test_default_branch_excludes_release_tagged_rows(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "_detect_current_release_line", lambda: None)
        capture = self._capture_queries(cocoindex_search, monkeypatch)

        cocoindex_search.search_code("ParseConfig", mode="hybrid")

        assert len(capture) == 2  # dense + bm25
        for sql, params in capture:
            assert "NOT LIKE" in sql
            assert "%@%" in params

    def test_explicit_branch_scopes_to_release_line(self, cocoindex_search, monkeypatch):
        capture = self._capture_queries(cocoindex_search, monkeypatch)

        cocoindex_search.search_code("ParseConfig", mode="hybrid", branch="v1.5")

        assert len(capture) == 2
        for sql, params in capture:
            assert "%@release-v1.5/%" in params

    def test_auto_detected_branch_scopes_to_release_line(self, cocoindex_search, monkeypatch):
        monkeypatch.setattr(cocoindex_search, "_detect_current_release_line", lambda: "v1.6")
        capture = self._capture_queries(cocoindex_search, monkeypatch)

        cocoindex_search.search_code("ParseConfig", mode="hybrid")

        assert len(capture) == 2
        for sql, params in capture:
            assert "%@release-v1.6/%" in params

    def test_repo_and_branch_combine(self, cocoindex_search, monkeypatch):
        capture = self._capture_queries(cocoindex_search, monkeypatch)

        cocoindex_search.search_code(
            "ParseConfig", mode="dense", repo="kubernaut-operator", branch="v1.5",
        )

        assert len(capture) == 1
        sql, params = capture[0]
        assert "kubernaut-operator@release-v1.5/%" in params


class TestPatternSearchCodeRepoScoping:
    def test_unknown_repo_short_circuits_without_touching_codepattern(self, cocoindex_search):
        """No roots match -> pattern_search_code returns [] before ever
        importing/using cocoindex.ops.code, so this needs no tree-sitter
        mocking to test the branch-scoping short-circuit path."""
        results = cocoindex_search.pattern_search_code(
            r"func \NAME(\(A*\)) error", "go", repo="nonexistent-repo",
        )
        assert results == []

    def test_unrecognized_branch_value_falls_back_to_main_root(self, cocoindex_search):
        """branch="v9.9" isn't in KUBERNAUT_RELEASE_LINES, so
        _resolve_release_line falls back to None (main) -- confirms an
        unrecognized branch value behaves exactly like the default/main
        case (the plain "kubernaut" root) rather than silently resolving to
        no roots at all."""
        release_line = cocoindex_search._resolve_release_line("v9.9")
        roots = cocoindex_search._select_pattern_roots("kubernaut", release_line)
        assert [r[0] for r in roots] == ["kubernaut"]
