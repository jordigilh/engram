"""Tests for hooks/_write_plan_marker.py -- the beforeSubmitPrompt
detector's helper that finds the newest confirmed plan file, extracts its
`overview` field, and writes the per-session marker consumed by
hooks/post-plan-hindsight-check.py. See docs/findings/2026-08.md for the
design history (in particular: why only `overview`, not full plan content).
"""
from __future__ import annotations

import json

import _write_plan_marker as wpm


class TestDetectProject:
    def test_kubernaut_path_detected(self):
        assert wpm.detect_project(["/Users/jgil/go/src/github.com/jordigilh/kubernaut"]) == "kubernaut"

    def test_kubernaut_sibling_repo_detected(self):
        assert wpm.detect_project(["/Users/jgil/go/src/github.com/jordigilh/kubernaut-operator"]) == "kubernaut"

    def test_dcm_project_repo_detected(self):
        assert wpm.detect_project(["/Users/jgil/go/src/github.com/dcm-project/control-plane"]) == "dcm"

    def test_unrelated_path_returns_none(self):
        assert wpm.detect_project(["/Users/jgil/go/src/github.com/jordigilh/engram"]) is None

    def test_empty_list_returns_none(self):
        assert wpm.detect_project([]) is None

    def test_first_matching_root_wins(self):
        assert wpm.detect_project([
            "/Users/jgil/some/other/repo",
            "/Users/jgil/go/src/github.com/dcm-project/cli",
        ]) == "dcm"


class TestDetectRepoName:
    def test_extracts_basename_of_first_root(self):
        assert wpm.detect_repo_name(
            ["/Users/jgil/go/src/github.com/dcm-project/osac-service-provider"]
        ) == "osac-service-provider"

    def test_strips_trailing_slash(self):
        assert wpm.detect_repo_name(
            ["/Users/jgil/go/src/github.com/jordigilh/kubernaut/"]
        ) == "kubernaut"

    def test_empty_list_returns_none(self):
        assert wpm.detect_repo_name([]) is None

    def test_skips_empty_strings_to_find_a_real_root(self):
        assert wpm.detect_repo_name(["", "/Users/jgil/go/src/github.com/dcm-project/cli"]) == "cli"

    def test_generic_not_tied_to_dcm_or_kubernaut_families(self):
        """Unlike detect_project(), this has no project-family allowlist --
        any repo basename is returned, since the checklist-reminder feature
        scopes itself by whether a matching file exists, not by this
        function pre-filtering to known projects."""
        assert wpm.detect_repo_name(["/Users/jgil/go/src/github.com/someone-else/random-repo"]) == "random-repo"


def _write_plan(plans_dir, filename, overview, extra_after_overview="todos:\n  - id: x\n"):
    plans_dir.mkdir(parents=True, exist_ok=True)
    content = f'---\nname: Test Plan\noverview: "{overview}"\n{extra_after_overview}---\n\n# Body\n'
    (plans_dir / filename).write_text(content)


class TestMain:
    def test_no_args_is_noop(self, monkeypatch, tmp_path):
        marker_dir = tmp_path / "markers"
        monkeypatch.setattr(wpm, "MARKER_DIR", marker_dir)
        monkeypatch.setattr("sys.argv", ["prog"])
        assert wpm.main() == 0
        assert not marker_dir.exists()

    def test_missing_session_id_is_noop(self, monkeypatch, tmp_path):
        marker_dir = tmp_path / "markers"
        monkeypatch.setattr(wpm, "MARKER_DIR", marker_dir)
        monkeypatch.setattr("sys.argv", ["prog", "", "/some/workspace"])
        assert wpm.main() == 0
        assert not marker_dir.exists()

    def test_no_plan_files_is_noop(self, monkeypatch, tmp_path):
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        marker_dir = tmp_path / "markers"
        monkeypatch.setattr(wpm, "PLANS_DIR", plans_dir)
        monkeypatch.setattr(wpm, "MARKER_DIR", marker_dir)
        monkeypatch.setattr("sys.argv", ["prog", "sess-1", "/some/workspace"])
        assert wpm.main() == 0
        assert not marker_dir.exists()

    def test_plan_with_no_overview_field_is_noop(self, monkeypatch, tmp_path):
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        (plans_dir / "no_overview.plan.md").write_text("---\nname: X\ntodos: []\n---\nbody")
        marker_dir = tmp_path / "markers"
        monkeypatch.setattr(wpm, "PLANS_DIR", plans_dir)
        monkeypatch.setattr(wpm, "MARKER_DIR", marker_dir)
        monkeypatch.setattr("sys.argv", ["prog", "sess-1", "/some/workspace"])
        assert wpm.main() == 0
        assert not marker_dir.exists()

    def test_writes_marker_with_overview_and_project(self, monkeypatch, tmp_path):
        plans_dir = tmp_path / "plans"
        _write_plan(plans_dir, "a.plan.md", "Do the thing carefully.")
        marker_dir = tmp_path / "markers"
        monkeypatch.setattr(wpm, "PLANS_DIR", plans_dir)
        monkeypatch.setattr(wpm, "MARKER_DIR", marker_dir)
        monkeypatch.setattr(
            "sys.argv",
            ["prog", "sess-42", "/Users/jgil/go/src/github.com/jordigilh/kubernaut"],
        )

        assert wpm.main() == 0

        marker_path = marker_dir / "plan-kickoff-sess-42.json"
        assert marker_path.exists()
        data = json.loads(marker_path.read_text())
        assert data["overview"] == "Do the thing carefully."
        assert data["project"] == "kubernaut"
        assert data["repo"] == "kubernaut"
        assert data["plan_file"].endswith("a.plan.md")

    def test_picks_newest_plan_by_mtime(self, monkeypatch, tmp_path):
        import os
        import time

        plans_dir = tmp_path / "plans"
        _write_plan(plans_dir, "older.plan.md", "Older plan overview.")
        time.sleep(0.05)
        _write_plan(plans_dir, "newer.plan.md", "Newer plan overview.")
        # Force distinguishable mtimes regardless of filesystem timestamp resolution.
        os.utime(plans_dir / "older.plan.md", (1000, 1000))
        os.utime(plans_dir / "newer.plan.md", (2000, 2000))

        marker_dir = tmp_path / "markers"
        monkeypatch.setattr(wpm, "PLANS_DIR", plans_dir)
        monkeypatch.setattr(wpm, "MARKER_DIR", marker_dir)
        monkeypatch.setattr("sys.argv", ["prog", "sess-1", ""])

        wpm.main()

        data = json.loads((marker_dir / "plan-kickoff-sess-1.json").read_text())
        assert data["overview"] == "Newer plan overview."

    def test_overview_with_no_workspace_project_match_is_none(self, monkeypatch, tmp_path):
        plans_dir = tmp_path / "plans"
        _write_plan(plans_dir, "a.plan.md", "Some overview.")
        marker_dir = tmp_path / "markers"
        monkeypatch.setattr(wpm, "PLANS_DIR", plans_dir)
        monkeypatch.setattr(wpm, "MARKER_DIR", marker_dir)
        monkeypatch.setattr("sys.argv", ["prog", "sess-1", "/Users/jgil/go/src/github.com/jordigilh/engram"])

        wpm.main()

        data = json.loads((marker_dir / "plan-kickoff-sess-1.json").read_text())
        assert data["project"] is None
