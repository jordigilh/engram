"""Tests for report.py's normalize_server_name() / aggregate_mcp_calls()
(lever #4 of the 2026-07-14 "reduce input tokens" review), and for
collect_ingestion_coverage()'s per-project GitHub issues/PRs scoping and
PROJECT_CONFIGS additions from the 2026-07-15 Engram onboarding +
kubernaut-operator/console tag-scoped recall work.

Cursor prepends a project-workspace prefix (and sometimes an mcpScope
suffix) to MCP server names at call time, fragmenting one correctly
bank-scoped tool's hit-rate stats across near-duplicate rows. These tests
pin the exact prefix/suffix patterns confirmed against live mcp-calls.jsonl
data during the spike (see docs/FINDINGS.md), including the deliberate
carve-out for "user-*" names, which the same spike found correlates with a
real 100%-miss anomaly rather than a cosmetic rename.
"""
from __future__ import annotations

import subprocess
import urllib.request

import report


class TestNormalizeServerName:
    def test_plain_name_is_unchanged(self):
        assert report.normalize_server_name("hindsight-docs") == "hindsight-docs"
        assert report.normalize_server_name("cocoindex-code") == "cocoindex-code"
        assert report.normalize_server_name("gopls") == "gopls"

    def test_project_prefix_is_stripped(self):
        assert report.normalize_server_name("kubernaut-hindsight-docs") == "hindsight-docs"
        assert report.normalize_server_name("kubernaut-hindsight-issues") == "hindsight-issues"
        assert report.normalize_server_name("enhancements-hindsight-issues") == "hindsight-issues"

    def test_project_prefix_with_version_segment_is_stripped(self):
        assert report.normalize_server_name("kubernaut-v1.6-hindsight-docs") == "hindsight-docs"
        assert report.normalize_server_name("kubernaut-v1.6-hindsight-issues") == "hindsight-issues"

    def test_bare_hindsight_with_version_prefix_is_stripped(self):
        """Regression guard: "kubernaut-v1.6-hindsight" has no -docs/-issues
        suffix at all, just the base recall tool name -- the prefix regex's
        lookahead must still match "hindsight" on its own, not only when
        followed by "-docs"/"-issues"."""
        assert report.normalize_server_name("project-0-kubernaut-v1.6-hindsight") == "hindsight"

    def test_project_index_prefix_is_stripped(self):
        assert report.normalize_server_name("project-0-kubernaut-hindsight-docs") == "hindsight-docs"
        assert report.normalize_server_name("project-0-kubernaut-cocoindex-code") == "cocoindex-code"

    def test_mcp_scope_suffix_is_stripped(self):
        raw = "project-0-kubernaut-cocoindex-code::mcpScope:profile:ZGVmYXVsdA:project:NTM4M2E5Y2I:cfg:MzU2MjlhNg"
        assert report.normalize_server_name(raw) == "cocoindex-code"

    def test_regression_user_prefix_is_never_stripped(self):
        """Regression guard: live data showed every "user-*" call across
        three tool families was a 100% miss (6/6) -- a real binding anomaly,
        not a cosmetic rename like the project-prefixed variants. Stripping
        it would silently dilute that signal into the main bucket's hit
        rate instead of keeping it visible as its own row."""
        assert report.normalize_server_name("user-cocoindex-code") == "user-cocoindex-code"
        assert report.normalize_server_name("user-hindsight-docs") == "user-hindsight-docs"
        assert report.normalize_server_name("user-hindsight-issues") == "user-hindsight-issues"

    def test_names_with_no_known_tool_suffix_are_unchanged(self):
        assert report.normalize_server_name("unknown") == "unknown"
        assert report.normalize_server_name("cursor-app-control") == "cursor-app-control"
        assert report.normalize_server_name("cursor-ide-browser") == "cursor-ide-browser"


class TestAggregateMcpCallsNormalization:
    def _call(self, server, hit):
        return {"server": server, "hit": hit, "ts": "2026-07-14T00:00:00"}

    def test_regression_near_duplicate_names_roll_up_into_one_row(self):
        """Regression guard for the original bug: before normalization,
        these 4 calls would fragment into 3 separate by_server rows even
        though they're all the same correctly-bound hindsight-docs tool."""
        entries = [
            self._call("hindsight-docs", True),
            self._call("kubernaut-hindsight-docs", True),
            self._call("kubernaut-v1.6-hindsight-docs", True),
            self._call("project-0-kubernaut-hindsight-docs", True),
        ]

        agg = report.aggregate_mcp_calls(entries)

        assert set(agg["by_server"].keys()) == {"hindsight-docs"}
        assert agg["by_server"]["hindsight-docs"]["calls"] == 4
        assert agg["by_server"]["hindsight-docs"]["hit_rate"] == 1.0

    def test_regression_user_scoped_misses_stay_visible_not_diluted(self):
        """Regression guard: folding "user-cocoindex-code" into the main
        "cocoindex-code" bucket would dilute a 100%-miss anomaly down to a
        barely-noticeable dip in an otherwise-healthy hit rate."""
        entries = (
            [self._call("cocoindex-code", True) for _ in range(9)]
            + [self._call("user-cocoindex-code", False) for _ in range(4)]
        )

        agg = report.aggregate_mcp_calls(entries)

        assert agg["by_server"]["cocoindex-code"]["hit_rate"] == 1.0
        assert agg["by_server"]["user-cocoindex-code"]["calls"] == 4
        assert agg["by_server"]["user-cocoindex-code"]["hit_rate"] == 0.0

    def test_by_day_trend_also_uses_normalized_names(self):
        entries = [
            self._call("kubernaut-hindsight-docs", True),
            self._call("hindsight-docs", True),
        ]

        agg = report.aggregate_mcp_calls(entries)

        assert list(agg["by_day"]["2026-07-14"].keys()) == ["hindsight-docs"]
        assert agg["by_day"]["2026-07-14"]["hindsight-docs"] == 2


class TestProjectConfigsEngram:
    """Mirrors nightly-learn.py's PROJECT_CONFIGS additions from the
    2026-07-15 Engram onboarding -- report.py keeps its own copy, so it can
    drift independently if only one file is edited."""

    def test_engram_config_has_no_issues_repos(self):
        assert "issues_repos" not in report.PROJECT_CONFIGS["engram"]

    def test_engram_config_has_required_keys(self):
        cfg = report.PROJECT_CONFIGS["engram"]
        assert cfg["log_suffix"] == "-engram"
        assert cfg["workspace_prefixes"] == ["Users-jgil-go-src-github-com-jordigilh-engram"]
        assert "engram-docs" in cfg["banks"]

    def test_kubernaut_and_dcm_declare_issues_repos(self):
        assert "jordigilh/kubernaut" in report.PROJECT_CONFIGS["kubernaut"]["issues_repos"]
        assert "dcm-project/dcm" in report.PROJECT_CONFIGS["dcm"]["issues_repos"]


class TestCollectIngestionCoverageProjectScoping:
    """Regression guard for the 2026-07-15 fix: collect_ingestion_coverage()
    used to hardcode a single "jordigilh/kubernaut" gh query, so DCM's (and
    every other project's) GitHub issues/PRs total was always zero. It now
    iterates PROJECT_CONFIGS[project]["issues_repos"] when a project is
    given, or every configured project's repos combined when it is not.
    """

    def _fake_gh_run(self, queried_repos: list[str]):
        def _run(cmd, **kwargs):
            if "--repo" in cmd:
                queried_repos.append(cmd[cmd.index("--repo") + 1])
            result = subprocess.CompletedProcess(cmd, returncode=0, stdout="3\n", stderr="")
            return result
        return _run

    def test_kubernaut_project_only_queries_kubernaut_repos(self, monkeypatch):
        queried: list[str] = []
        monkeypatch.setattr(subprocess, "run", self._fake_gh_run(queried))
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no network in test")))

        coverage = report.collect_ingestion_coverage(project="kubernaut")

        expected_repos = report.PROJECT_CONFIGS["kubernaut"]["issues_repos"]
        assert len(queried) == 2 * len(expected_repos)  # issues + prs loops
        assert set(queried) == set(expected_repos)
        assert coverage["issues"]["total"] == 3 * len(expected_repos)

    def test_dcm_project_only_queries_dcm_repos(self, monkeypatch):
        queried: list[str] = []
        monkeypatch.setattr(subprocess, "run", self._fake_gh_run(queried))
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no network in test")))

        coverage = report.collect_ingestion_coverage(project="dcm")

        expected_repos = report.PROJECT_CONFIGS["dcm"]["issues_repos"]
        assert len(queried) == 2 * len(expected_repos)  # issues + prs loops
        assert set(queried) == set(expected_repos)
        assert coverage["issues"]["total"] == 3 * len(expected_repos)

    def test_engram_project_contributes_nothing_to_issues_total(self, monkeypatch):
        """Engram has no issues_repos -- collect_ingestion_coverage() must
        not crash, and must not report a bogus issues/prs total for it."""
        queried: list[str] = []
        monkeypatch.setattr(subprocess, "run", self._fake_gh_run(queried))
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no network in test")))

        coverage = report.collect_ingestion_coverage(project="engram")

        assert queried == []
        assert "issues" not in coverage
        assert "prs" not in coverage

    def test_no_project_sums_across_all_configured_repos(self, monkeypatch):
        queried: list[str] = []
        monkeypatch.setattr(subprocess, "run", self._fake_gh_run(queried))
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no network in test")))

        coverage = report.collect_ingestion_coverage(project=None)

        all_repos = [r for cfg in report.PROJECT_CONFIGS.values() for r in cfg.get("issues_repos", [])]
        assert coverage["issues"]["total"] == 3 * len(all_repos)

    def test_engram_bank_and_pgvector_keys_are_populated_from_hindsight_api(self, monkeypatch):
        """Bank/pgvector counts (unlike issues/prs) are always reported for
        every known project regardless of the project= filter."""
        monkeypatch.setattr(subprocess, "run", self._fake_gh_run([]))

        def fake_urlopen(url, timeout=10):
            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b'{"total_documents": 42}'
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        coverage = report.collect_ingestion_coverage(project="kubernaut")

        assert coverage["engram_docs_indexed"] == 42
