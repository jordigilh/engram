"""Tests for retain-now.py: the on-demand, single-session retain path
(checkpoint-style) that reuses nightly-learn.py's extraction/contradiction/
watermark pipeline unchanged, scoped to exactly one transcript file instead
of a periodic sweep across every onboarded workspace.

Corrects the premise nightly-learn.py's hourly job already retains within
~1-2h -- this closes the narrower remaining gap (determinism + session
scoping), not overnight latency. See docs/FINDINGS.md 2026-07-29 and GitHub
issue #4.
"""
from __future__ import annotations

import json

import pytest


def _write_transcript(path, messages):
    with open(path, "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")


def _user(text):
    return {"role": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(text):
    return {"role": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


CORRECTION_MESSAGES = [
    _user("please add a retry loop"),
    _assistant("done, added a retry loop"),
    _user("no, that's wrong, we don't use retry loops here"),
    _assistant("understood, removing it"),
]


class TestResolveSessionPath:
    def test_resolves_existing_file_path_directly(self, retain_now, tmp_path):
        f = tmp_path / "t1.jsonl"
        f.write_text("{}")

        assert retain_now.resolve_session_path(str(f)) == f

    def test_resolves_bare_transcript_id_by_glob(self, retain_now, nightly_learn, tmp_path, monkeypatch):
        transcript_dir = (
            tmp_path / "Users-jgil-go-src-github-com-jordigilh-engram" / "agent-transcripts"
        )
        transcript_dir.mkdir(parents=True)
        f = transcript_dir / "abc-123.jsonl"
        f.write_text("{}")
        monkeypatch.setattr(
            nightly_learn, "TRANSCRIPTS_GLOB",
            str(tmp_path / "*" / "agent-transcripts" / "**" / "*.jsonl"),
        )

        assert retain_now.resolve_session_path("abc-123") == f

    def test_unresolvable_session_returns_none(self, retain_now, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(
            nightly_learn, "TRANSCRIPTS_GLOB",
            str(tmp_path / "*" / "agent-transcripts" / "**" / "*.jsonl"),
        )

        assert retain_now.resolve_session_path("does-not-exist") is None


class TestRunOnce:
    def test_retains_new_correction_and_returns_summary(self, retain_now, nightly_learn, tmp_path, monkeypatch):
        path = tmp_path / "t1.jsonl"
        _write_transcript(path, CORRECTION_MESSAGES)
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: "don't use" in text)
        monkeypatch.setattr(nightly_learn, "is_instruction", lambda text: False)
        monkeypatch.setattr(nightly_learn.contradiction_resolution, "resolve",
                             lambda *a, **k: nightly_learn.contradiction_resolution.Resolution(action="retain"))
        posted = []
        monkeypatch.setattr(nightly_learn, "api_post",
                             lambda p, payload: posted.append(payload) or {"success": True, "items_count": 1, "usage": {}})
        monkeypatch.setattr(nightly_learn, "project_for_transcript_path", lambda p: None)

        result = retain_now.run_once(path, watermarks={}, seen_hashes=set())

        assert result["corrections_detected"] == 1
        assert result["instructions_detected"] == 0
        assert result["items_retained"] == 1
        assert len(posted) == 1

    def test_uses_existing_watermark_as_start_index(self, retain_now, nightly_learn, tmp_path, monkeypatch):
        """Only messages past the last-known watermark should be scanned --
        mirrors filter_and_scan()'s own incremental-scan contract."""
        path = tmp_path / "t1.jsonl"
        _write_transcript(path, CORRECTION_MESSAGES)
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: "don't use" in text)
        monkeypatch.setattr(nightly_learn, "is_instruction", lambda text: False)
        monkeypatch.setattr(nightly_learn.contradiction_resolution, "resolve",
                             lambda *a, **k: nightly_learn.contradiction_resolution.Resolution(action="retain"))
        monkeypatch.setattr(nightly_learn, "api_post",
                             lambda p, payload: {"success": True, "items_count": 1, "usage": {}})
        monkeypatch.setattr(nightly_learn, "project_for_transcript_path", lambda p: None)

        # Watermark says the first 4 messages (the whole file) were already
        # processed by a prior hourly/on-demand run.
        watermarks = {"t1": {"size": path.stat().st_size, "message_count": 4}}

        result = retain_now.run_once(path, watermarks=watermarks, seen_hashes=set())

        assert result["corrections_detected"] == 0
        assert result["items_retained"] == 0

    def test_updates_watermark_so_next_scan_sees_nothing_new(self, retain_now, nightly_learn, tmp_path, monkeypatch):
        """The idempotency acceptance criterion: after an on-demand retain,
        nightly-learn.py's own filter_and_scan() must not re-process the
        same content on its next scheduled sweep."""
        path = tmp_path / "t1.jsonl"
        _write_transcript(path, CORRECTION_MESSAGES)
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: "don't use" in text)
        monkeypatch.setattr(nightly_learn, "is_instruction", lambda text: False)
        monkeypatch.setattr(nightly_learn.contradiction_resolution, "resolve",
                             lambda *a, **k: nightly_learn.contradiction_resolution.Resolution(action="retain"))
        monkeypatch.setattr(nightly_learn, "api_post",
                             lambda p, payload: {"success": True, "items_count": 1, "usage": {}})
        monkeypatch.setattr(nightly_learn, "project_for_transcript_path", lambda p: None)

        watermarks: dict = {}
        retain_now.run_once(path, watermarks=watermarks, seen_hashes=set())

        candidates = nightly_learn.filter_and_scan([path], watermarks)

        assert candidates == []

    def test_forwards_project_override(self, retain_now, nightly_learn, tmp_path, monkeypatch):
        path = tmp_path / "t1.jsonl"
        _write_transcript(path, CORRECTION_MESSAGES)
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: "don't use" in text)
        monkeypatch.setattr(nightly_learn, "is_instruction", lambda text: False)
        monkeypatch.setattr(nightly_learn.contradiction_resolution, "resolve",
                             lambda *a, **k: nightly_learn.contradiction_resolution.Resolution(action="retain"))
        posted = []
        monkeypatch.setattr(nightly_learn, "api_post",
                             lambda p, payload: posted.append(payload) or {"success": True, "items_count": 1, "usage": {}})
        # If auto-resolution falls back to path-based resolution instead of
        # honoring the override, this would return a different project and
        # the assertion below would catch it.
        monkeypatch.setattr(nightly_learn, "project_for_transcript_path", lambda p: "kubernaut")

        result = retain_now.run_once(path, watermarks={}, seen_hashes=set(), project_override="engram")

        assert result["project"] == "engram"
        assert posted[0]["items"][0]["tags"] == ["engram", "CORRECTION"] or "engram" in posted[0]["items"][0].get("tags", [])

    def test_falls_back_to_project_for_transcript_path_when_no_override(self, retain_now, nightly_learn, tmp_path, monkeypatch):
        path = tmp_path / "t1.jsonl"
        _write_transcript(path, CORRECTION_MESSAGES)
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: "don't use" in text)
        monkeypatch.setattr(nightly_learn, "is_instruction", lambda text: False)
        monkeypatch.setattr(nightly_learn.contradiction_resolution, "resolve",
                             lambda *a, **k: nightly_learn.contradiction_resolution.Resolution(action="retain"))
        monkeypatch.setattr(nightly_learn, "api_post",
                             lambda p, payload: {"success": True, "items_count": 1, "usage": {}})
        monkeypatch.setattr(nightly_learn, "project_for_transcript_path", lambda p: "dcm")

        result = retain_now.run_once(path, watermarks={}, seen_hashes=set())

        assert result["project"] == "dcm"


class TestMain:
    def test_returns_1_when_session_unresolvable(self, retain_now, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(
            nightly_learn, "TRANSCRIPTS_GLOB",
            str(tmp_path / "*" / "agent-transcripts" / "**" / "*.jsonl"),
        )
        monkeypatch.setattr(nightly_learn, "load_watermarks", lambda: {})
        monkeypatch.setattr(nightly_learn, "load_retained_hashes", lambda: set())

        exit_code = retain_now.main(["--session", "does-not-exist"])

        assert exit_code == 1

    def test_success_path_loads_and_persists_state(self, retain_now, nightly_learn, tmp_path, monkeypatch):
        path = tmp_path / "t1.jsonl"
        _write_transcript(path, CORRECTION_MESSAGES)

        watermarks: dict = {}
        seen_hashes: set = set()
        saved = {"watermarks": None, "hashes": None}
        monkeypatch.setattr(nightly_learn, "load_watermarks", lambda: watermarks)
        monkeypatch.setattr(nightly_learn, "load_retained_hashes", lambda: seen_hashes)
        monkeypatch.setattr(nightly_learn, "save_watermarks", lambda wm: saved.__setitem__("watermarks", dict(wm)))
        monkeypatch.setattr(nightly_learn, "save_retained_hashes", lambda h: saved.__setitem__("hashes", set(h)))
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: "don't use" in text)
        monkeypatch.setattr(nightly_learn, "is_instruction", lambda text: False)
        monkeypatch.setattr(nightly_learn.contradiction_resolution, "resolve",
                             lambda *a, **k: nightly_learn.contradiction_resolution.Resolution(action="retain"))
        monkeypatch.setattr(nightly_learn, "api_post",
                             lambda p, payload: {"success": True, "items_count": 1, "usage": {}})
        monkeypatch.setattr(nightly_learn, "project_for_transcript_path", lambda p: None)

        exit_code = retain_now.main(["--session", str(path), "--project", "engram"])

        assert exit_code == 0
        assert saved["watermarks"] is not None
        assert saved["hashes"] is not None
        assert saved["watermarks"]["t1"]["message_count"] == len(CORRECTION_MESSAGES)
