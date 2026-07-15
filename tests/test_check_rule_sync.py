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


class TestRulePairsRealShape:
    """Pins the real (non-monkeypatched) RULE_PAIRS dict so a future edit
    can't silently drop the engram/operator/console pairs added during the
    2026-07-15 generalization -- TestMain below only ever exercises fake
    pairs, so it wouldn't catch that regression on its own."""

    def test_all_four_pairs_are_registered(self, check_rule_sync):
        assert set(check_rule_sync.RULE_PAIRS.keys()) == {"global", "engram", "operator", "console"}

    def test_each_pair_is_a_two_tuple_of_paths(self, check_rule_sync):
        for name, pair in check_rule_sync.RULE_PAIRS.items():
            assert len(pair) == 2, f"{name} pair must be (canonical, deployed)"

    def test_operator_and_console_deploy_to_their_own_repos(self, check_rule_sync):
        _, operator_deployed = check_rule_sync.RULE_PAIRS["operator"]
        _, console_deployed = check_rule_sync.RULE_PAIRS["console"]
        assert "kubernaut-operator" in str(operator_deployed)
        assert "kubernaut-console" in str(console_deployed)

    def test_engram_deploys_within_this_repo(self, check_rule_sync):
        canonical, deployed = check_rule_sync.RULE_PAIRS["engram"]
        assert str(canonical).endswith("cursor/engram-hindsight-memory.mdc")
        assert str(deployed).endswith(".cursor/rules/hindsight-memory.mdc")


class TestMain:
    """main() now iterates RULE_PAIRS (a name -> (canonical, deployed) dict)
    instead of a single hardcoded CANONICAL/DEPLOYED pair, so tests
    monkeypatch RULE_PAIRS directly -- see check-rule-sync.py's 2026-07-15
    generalization to cover engram/operator/console alongside the original
    global rule pair."""

    def test_fix_copies_canonical_over_deployed_on_drift(self, check_rule_sync, tmp_path, monkeypatch):
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        canonical.write_text("new text\n")
        deployed.write_text("old text\n")
        monkeypatch.setattr(check_rule_sync, "RULE_PAIRS", {"global": (canonical, deployed)})
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py", "--fix"])

        exit_code = check_rule_sync.main()

        assert exit_code == 0
        assert deployed.read_text() == "new text\n"

    def test_without_fix_flag_drift_returns_nonzero_and_leaves_deployed_unchanged(self, check_rule_sync, tmp_path, monkeypatch):
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        canonical.write_text("new text\n")
        deployed.write_text("old text\n")
        monkeypatch.setattr(check_rule_sync, "RULE_PAIRS", {"global": (canonical, deployed)})
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py"])

        exit_code = check_rule_sync.main()

        assert exit_code == 1
        assert deployed.read_text() == "old text\n"

    def test_in_sync_returns_zero(self, check_rule_sync, tmp_path, monkeypatch):
        canonical = tmp_path / "canonical.mdc"
        deployed = tmp_path / "deployed.mdc"
        canonical.write_text("same\n")
        deployed.write_text("same\n")
        monkeypatch.setattr(check_rule_sync, "RULE_PAIRS", {"global": (canonical, deployed)})
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py"])

        assert check_rule_sync.main() == 0

    def test_missing_file_returns_two(self, check_rule_sync, tmp_path, monkeypatch):
        canonical = tmp_path / "missing.mdc"
        deployed = tmp_path / "deployed.mdc"
        deployed.write_text("content\n")
        monkeypatch.setattr(check_rule_sync, "RULE_PAIRS", {"global": (canonical, deployed)})
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py"])

        assert check_rule_sync.main() == 2

    def test_checks_all_pairs_by_default_and_aggregates_worst_exit_code(self, check_rule_sync, tmp_path, monkeypatch):
        """One pair in sync, one with drift -- overall exit code reflects the
        worst individual result (drift), and both pairs are still reported."""
        c1, d1 = tmp_path / "c1.mdc", tmp_path / "d1.mdc"
        c2, d2 = tmp_path / "c2.mdc", tmp_path / "d2.mdc"
        c1.write_text("same\n")
        d1.write_text("same\n")
        c2.write_text("new\n")
        d2.write_text("old\n")
        monkeypatch.setattr(check_rule_sync, "RULE_PAIRS", {"alpha": (c1, d1), "beta": (c2, d2)})
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py"])

        exit_code = check_rule_sync.main()

        assert exit_code == 1
        assert d2.read_text() == "old\n"

    def test_pair_flag_checks_only_the_named_pair(self, check_rule_sync, tmp_path, monkeypatch):
        """--pair beta with drift should be reported/exit nonzero even though
        alpha (unchecked) is missing entirely -- proves only beta was touched."""
        c1, d1 = tmp_path / "missing-c1.mdc", tmp_path / "missing-d1.mdc"
        c2, d2 = tmp_path / "c2.mdc", tmp_path / "d2.mdc"
        c2.write_text("new\n")
        d2.write_text("old\n")
        monkeypatch.setattr(check_rule_sync, "RULE_PAIRS", {"alpha": (c1, d1), "beta": (c2, d2)})
        monkeypatch.setattr("sys.argv", ["check-rule-sync.py", "--pair", "beta", "--fix"])

        exit_code = check_rule_sync.main()

        assert exit_code == 0
        assert d2.read_text() == "new\n"
