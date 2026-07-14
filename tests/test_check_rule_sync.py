"""Tests for check-rule-sync.py -- lever #6 of the 2026-07-14 "reduce input
tokens" review. hindsight-memory.mdc is deployed once globally to
~/.cursor/rules/, separately from the canonical copy under version control
here (cursor/hindsight-memory.mdc), so the two can silently drift.
"""
from __future__ import annotations


class TestCheckRuleSync:
    def test_identical_files_are_in_sync(self, check_rule_sync, tmp_path):
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        canonical.write_text("same content\n")
        deployed.write_text("same content\n")

        result = check_rule_sync.check_rule_sync(canonical, deployed)

        assert result["in_sync"] is True
        assert result["diff"] == []
        assert result["missing"] == []

    def test_differing_files_report_drift_with_diff(self, check_rule_sync, tmp_path):
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        canonical.write_text("line one\nline two (canonical)\n")
        deployed.write_text("line one\nline two (deployed)\n")

        result = check_rule_sync.check_rule_sync(canonical, deployed)

        assert result["in_sync"] is False
        assert result["missing"] == []
        diff_text = "".join(result["diff"])
        assert "line two (canonical)" in diff_text
        assert "line two (deployed)" in diff_text

    def test_missing_canonical_is_reported_without_raising(self, check_rule_sync, tmp_path):
        canonical = tmp_path / "missing-canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        deployed.write_text("content\n")

        result = check_rule_sync.check_rule_sync(canonical, deployed)

        assert result["in_sync"] is None
        assert str(canonical) in result["missing"]

    def test_missing_deployed_is_reported_without_raising(self, check_rule_sync, tmp_path):
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "missing-deployed.mdc"
        canonical.write_text("content\n")

        result = check_rule_sync.check_rule_sync(canonical, deployed)

        assert result["in_sync"] is None
        assert str(deployed) in result["missing"]

    def test_regression_diff_direction_shows_deployed_as_from_and_canonical_as_to(self, check_rule_sync, tmp_path):
        """Regression guard: --fix always copies canonical -> deployed, so the
        diff must be presented in that same direction (deployed as the "from"
        / stale side, canonical as the "to" / source-of-truth side) or a
        human reading the diff before running --fix would see it backwards."""
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        canonical.write_text("new text\n")
        deployed.write_text("old text\n")

        result = check_rule_sync.check_rule_sync(canonical, deployed)

        diff_text = "".join(result["diff"])
        assert diff_text.startswith("--- ")
        from_line, to_line = diff_text.splitlines()[:2]
        assert "deployed.mdc" in from_line
        assert "canonical.mdc" in to_line
        assert "-old text" in diff_text
        assert "+new text" in diff_text


class TestMain:
    def test_fix_copies_canonical_over_deployed_on_drift(self, check_rule_sync, tmp_path, monkeypatch):
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        canonical.write_text("new text\n")
        deployed.write_text("old text\n")
        monkeypatch.setattr(check_rule_sync, "CANONICAL", canonical)
        monkeypatch.setattr(check_rule_sync, "DEPLOYED", deployed)
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py", "--fix"])

        exit_code = check_rule_sync.main()

        assert exit_code == 0
        assert deployed.read_text() == "new text\n"

    def test_without_fix_flag_drift_returns_nonzero_and_leaves_deployed_unchanged(self, check_rule_sync, tmp_path, monkeypatch):
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        canonical.write_text("new text\n")
        deployed.write_text("old text\n")
        monkeypatch.setattr(check_rule_sync, "CANONICAL", canonical)
        monkeypatch.setattr(check_rule_sync, "DEPLOYED", deployed)
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py"])

        exit_code = check_rule_sync.main()

        assert exit_code == 1
        assert deployed.read_text() == "old text\n"

    def test_in_sync_returns_zero(self, check_rule_sync, tmp_path, monkeypatch):
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        canonical.write_text("same\n")
        deployed.write_text("same\n")
        monkeypatch.setattr(check_rule_sync, "CANONICAL", canonical)
        monkeypatch.setattr(check_rule_sync, "DEPLOYED", deployed)
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py"])

        assert check_rule_sync.main() == 0

    def test_missing_file_returns_two(self, check_rule_sync, tmp_path, monkeypatch):
        canonical = tmp_path / "missing.mdc"
        deployed = tmp_path / "deployed.mdc"
        deployed.write_text("content\n")
        monkeypatch.setattr(check_rule_sync, "CANONICAL", canonical)
        monkeypatch.setattr(check_rule_sync, "DEPLOYED", deployed)
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py"])

        assert check_rule_sync.main() == 2
