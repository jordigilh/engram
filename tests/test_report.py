"""Tests for report.py's normalize_server_name() / aggregate_mcp_calls()
(lever #4 of the 2026-07-14 "reduce input tokens" review).

Cursor prepends a project-workspace prefix (and sometimes an mcpScope
suffix) to MCP server names at call time, fragmenting one correctly
bank-scoped tool's hit-rate stats across near-duplicate rows. These tests
pin the exact prefix/suffix patterns confirmed against live mcp-calls.jsonl
data during the spike (see docs/FINDINGS.md), including the deliberate
carve-out for "user-*" names, which the same spike found correlates with a
real 100%-miss anomaly rather than a cosmetic rename.
"""
from __future__ import annotations

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
