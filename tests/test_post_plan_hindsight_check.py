"""Tests for hooks/post-plan-hindsight-check.py -- the preToolUse enforcer
half of the Deterministic Correction Enforcement hook pair. See
docs/findings/2026-08.md for the full spike/preflight history behind this
design (why preToolUse+user_message instead of postToolUse+additional_context
or preToolUse+agent_message; why the marker is consumed before the check
runs, not after; why the query is the plan's `overview` field only).
"""
from __future__ import annotations

import json
import subprocess
from io import StringIO

import pytest


@pytest.fixture(autouse=True)
def isolate_paths(post_plan_hindsight_check, tmp_path, monkeypatch):
    monkeypatch.setattr(post_plan_hindsight_check, "MARKER_DIR", tmp_path / "markers")
    monkeypatch.setattr(post_plan_hindsight_check, "LOG_PATH", tmp_path / "logs" / "check.jsonl")
    # No repo has a checklist file by default -- individual tests that need
    # the has_checklist=True path create a file under this dir explicitly.
    monkeypatch.setattr(post_plan_hindsight_check, "REVIEW_CHECKLISTS_DIR", tmp_path / "review-checklists")


def _stdin(payload: dict) -> StringIO:
    return StringIO(json.dumps(payload))


class TestFastPaths:
    def test_missing_session_id_allows(self, post_plan_hindsight_check, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", _stdin({}))
        assert post_plan_hindsight_check.main() == 0
        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}

    def test_malformed_stdin_allows(self, post_plan_hindsight_check, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", StringIO("not json"))
        assert post_plan_hindsight_check.main() == 0
        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}

    def test_no_marker_file_allows_without_spawning_subprocess(self, post_plan_hindsight_check, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))

        def boom(*a, **k):
            raise AssertionError("subprocess should not be spawned when there's no marker")

        monkeypatch.setattr(subprocess, "run", boom)
        assert post_plan_hindsight_check.main() == 0
        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}

    def test_corrupt_marker_file_allows(self, post_plan_hindsight_check, monkeypatch, capsys):
        marker_dir = post_plan_hindsight_check.MARKER_DIR
        marker_dir.mkdir(parents=True)
        (marker_dir / "plan-kickoff-sess-1.json").write_text("not json")
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))

        assert post_plan_hindsight_check.main() == 0
        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}

    def test_marker_missing_overview_field_allows(self, post_plan_hindsight_check, monkeypatch, capsys):
        marker_dir = post_plan_hindsight_check.MARKER_DIR
        marker_dir.mkdir(parents=True)
        (marker_dir / "plan-kickoff-sess-1.json").write_text(json.dumps({"project": "kubernaut"}))
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))

        assert post_plan_hindsight_check.main() == 0
        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}


class TestMarkerConsumption:
    def _write_marker(self, post_plan_hindsight_check, session_id, overview="Do the thing.", project="kubernaut"):
        marker_dir = post_plan_hindsight_check.MARKER_DIR
        marker_dir.mkdir(parents=True, exist_ok=True)
        path = marker_dir / f"plan-kickoff-{session_id}.json"
        path.write_text(json.dumps({"overview": overview, "project": project}))
        return path

    def test_marker_deleted_before_check_runs_even_on_worker_crash(self, post_plan_hindsight_check, monkeypatch):
        marker_path = self._write_marker(post_plan_hindsight_check, "sess-1")
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))

        post_plan_hindsight_check.main()

        assert not marker_path.exists(), "marker must be consumed even when the worker subprocess fails"

    def test_second_call_with_same_session_id_is_a_fast_allow(self, post_plan_hindsight_check, monkeypatch, capsys):
        self._write_marker(post_plan_hindsight_check, "sess-1")

        fake_proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"action": "retain", "confidence": 0.0, "explanation": ""}),
            stderr="",
        )
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_proc)
        post_plan_hindsight_check.main()
        capsys.readouterr()  # discard first call's output

        # Second call, same session_id: marker is already gone -> fast allow, no subprocess.
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("subprocess should not run once the marker is consumed")
        ))
        assert post_plan_hindsight_check.main() == 0
        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}


class TestChecklistMarkerDeferral:
    """The blast-radius-critical behavior for the "Hook-delivered PR review
    checklist" feature: deletion timing must only change for repos that
    actually have a checklist file. Everything else (kubernaut, and any
    dcm-project repo without one) must be byte-identical to the pre-existing
    unconditional-immediate-unlink behavior."""

    def _write_marker(self, post_plan_hindsight_check, session_id="sess-1", repo=None, **extra):
        marker_dir = post_plan_hindsight_check.MARKER_DIR
        marker_dir.mkdir(parents=True, exist_ok=True)
        path = marker_dir / f"plan-kickoff-{session_id}.json"
        data = {"overview": "Do the thing.", "project": "dcm", **extra}
        if repo is not None:
            data["repo"] = repo
        path.write_text(json.dumps(data))
        return path

    def _fake_run(self, stdout_obj, returncode=0):
        return lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=json.dumps(stdout_obj), stderr="",
        )

    def test_no_checklist_file_for_repo_deletes_marker_immediately(self, post_plan_hindsight_check, monkeypatch):
        """Covers kubernaut and any dcm-project repo without a checklist
        file -- must match today's unconditional-unlink behavior exactly."""
        marker_path = self._write_marker(post_plan_hindsight_check, repo="kubernaut")
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", self._fake_run({"action": "retain", "confidence": 0.0, "explanation": ""}))

        post_plan_hindsight_check.main()

        assert not marker_path.exists()

    def test_marker_without_repo_field_deletes_immediately(self, post_plan_hindsight_check, monkeypatch):
        """Markers written before this feature existed have no "repo" key
        at all -- must degrade to the old behavior, not crash."""
        marker_path = self._write_marker(post_plan_hindsight_check, repo=None)
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", self._fake_run({"action": "retain", "confidence": 0.0, "explanation": ""}))

        post_plan_hindsight_check.main()

        assert not marker_path.exists()

    def test_checklist_file_exists_and_allow_outcome_defers_deletion(self, post_plan_hindsight_check, monkeypatch):
        checklists_dir = post_plan_hindsight_check.REVIEW_CHECKLISTS_DIR
        checklists_dir.mkdir(parents=True)
        (checklists_dir / "osac-service-provider.md").write_text("- some checklist item")
        marker_path = self._write_marker(post_plan_hindsight_check, repo="osac-service-provider")
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", self._fake_run({"action": "retain", "confidence": 0.0, "explanation": ""}))

        out = post_plan_hindsight_check.main()

        assert marker_path.exists(), "marker must survive an allow outcome so the reminder hook can consume it"
        assert out == 0

    def test_checklist_file_exists_but_timeout_still_defers_deletion(self, post_plan_hindsight_check, monkeypatch):
        checklists_dir = post_plan_hindsight_check.REVIEW_CHECKLISTS_DIR
        checklists_dir.mkdir(parents=True)
        (checklists_dir / "osac-service-provider.md").write_text("- some checklist item")
        marker_path = self._write_marker(post_plan_hindsight_check, repo="osac-service-provider")
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="worker", timeout=45)
        ))

        post_plan_hindsight_check.main()

        assert marker_path.exists(), "checklist reminder should still be able to fire even if the contradiction check timed out"

    def test_checklist_file_exists_but_deny_outcome_deletes_marker(self, post_plan_hindsight_check, monkeypatch, capsys):
        """postToolUse never fires after a preToolUse deny, so nothing would
        ever consume a marker left behind here -- must clean up immediately,
        same as the no-checklist-file path."""
        checklists_dir = post_plan_hindsight_check.REVIEW_CHECKLISTS_DIR
        checklists_dir.mkdir(parents=True)
        (checklists_dir / "osac-service-provider.md").write_text("- some checklist item")
        marker_path = self._write_marker(post_plan_hindsight_check, repo="osac-service-provider")
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", self._fake_run(
            {"action": "auto_resolved", "confidence": 0.9, "explanation": "conflict"}
        ))

        post_plan_hindsight_check.main()
        out = json.loads(capsys.readouterr().out)

        assert out["permission"] == "deny"
        assert not marker_path.exists()


class TestCheckOutcomes:
    def _write_marker(self, post_plan_hindsight_check, session_id="sess-1", overview="Do the thing.", project="kubernaut"):
        marker_dir = post_plan_hindsight_check.MARKER_DIR
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / f"plan-kickoff-{session_id}.json").write_text(
            json.dumps({"overview": overview, "project": project})
        )

    def _fake_run(self, stdout_obj, returncode=0):
        return lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=json.dumps(stdout_obj), stderr="",
        )

    def test_clean_result_allows(self, post_plan_hindsight_check, monkeypatch, capsys):
        self._write_marker(post_plan_hindsight_check)
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", self._fake_run({"action": "retain", "confidence": 0.0, "explanation": ""}))

        post_plan_hindsight_check.main()

        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}

    def test_auto_resolved_contradiction_denies_with_user_message_not_agent_message(
        self, post_plan_hindsight_check, monkeypatch, capsys,
    ):
        self._write_marker(post_plan_hindsight_check)
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", self._fake_run(
            {"action": "auto_resolved", "confidence": 0.92, "explanation": "conflicts with TDD RED-phase convention"}
        ))

        post_plan_hindsight_check.main()

        out = json.loads(capsys.readouterr().out)
        assert out["permission"] == "deny"
        assert "agent_message" not in out, "agent_message is confirmed broken on deny -- must use user_message only"
        assert "conflicts with TDD RED-phase convention" in out["user_message"]
        assert "0.92" in out["user_message"]

    def test_queued_contradiction_also_denies(self, post_plan_hindsight_check, monkeypatch, capsys):
        self._write_marker(post_plan_hindsight_check)
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", self._fake_run(
            {"action": "queued", "confidence": 0.6, "explanation": "ambiguous overlap"}
        ))

        post_plan_hindsight_check.main()

        out = json.loads(capsys.readouterr().out)
        assert out["permission"] == "deny"

    def test_worker_timeout_fails_open(self, post_plan_hindsight_check, monkeypatch, capsys):
        self._write_marker(post_plan_hindsight_check)
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))

        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="worker", timeout=15)

        monkeypatch.setattr(subprocess, "run", raise_timeout)

        post_plan_hindsight_check.main()

        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}

    def test_worker_nonzero_exit_fails_open(self, post_plan_hindsight_check, monkeypatch, capsys):
        self._write_marker(post_plan_hindsight_check)
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", self._fake_run({}, returncode=1))

        post_plan_hindsight_check.main()

        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}

    def test_worker_unparseable_stdout_fails_open(self, post_plan_hindsight_check, monkeypatch, capsys):
        self._write_marker(post_plan_hindsight_check)
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""),
        )

        post_plan_hindsight_check.main()

        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}

    def test_outcomes_are_logged(self, post_plan_hindsight_check, monkeypatch):
        self._write_marker(post_plan_hindsight_check)
        monkeypatch.setattr("sys.stdin", _stdin({"session_id": "sess-1"}))
        monkeypatch.setattr(subprocess, "run", self._fake_run(
            {"action": "auto_resolved", "confidence": 0.92, "explanation": "conflict"}
        ))

        post_plan_hindsight_check.main()

        log_lines = post_plan_hindsight_check.LOG_PATH.read_text().splitlines()
        assert len(log_lines) == 1
        entry = json.loads(log_lines[0])
        assert entry["outcome"] == "denied"
        assert entry["project"] == "kubernaut"
        assert entry["confidence"] == 0.92


class TestCrashSafety:
    def test_uncaught_exception_in_main_still_allows(self, post_plan_hindsight_check, monkeypatch, capsys):
        def boom():
            raise RuntimeError("totally unexpected")

        monkeypatch.setattr(post_plan_hindsight_check, "main", boom)

        assert post_plan_hindsight_check.run_main_safely() == 0
        assert json.loads(capsys.readouterr().out) == {"permission": "allow"}

    def test_crash_is_logged(self, post_plan_hindsight_check, monkeypatch):
        monkeypatch.setattr(post_plan_hindsight_check, "main", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        post_plan_hindsight_check.run_main_safely()

        log_lines = post_plan_hindsight_check.LOG_PATH.read_text().splitlines()
        entry = json.loads(log_lines[0])
        assert entry["outcome"] == "crash"
        assert "boom" in entry["error"]
