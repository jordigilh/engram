"""Tests for cocoindex-flows.py's process_transcript() -- the CocoIndex-side
mirror of nightly-learn.py's retain_windows() three-tier contradiction
branching. Regression coverage for the 2026-07-13 bug where "queued" items
were retained immediately, before human review.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import contradiction_resolution as cr


class FakeFile:
    """Minimal stand-in for CocoIndex's localfs.File.

    Mirrors the real cocoindex.resources.file.FilePath contract, which this
    test previously got wrong: `.path` is the *relative* path (a PurePath --
    no filesystem methods like .resolve()); `.resolve()` on the FilePath
    object itself is what returns the absolute concrete Path. These are NOT
    interchangeable -- confusing them (`.path.resolve()` instead of
    `.resolve()`) passed this mock's old version (a bare SimpleNamespace
    wrapping a concrete Path under `.path`) but threw AttributeError the
    moment real production code hit it. See docs/FINDINGS.md 2026-07-21.
    """

    def __init__(self, content: str, transcript_id: str = "tid-1", abs_path: Path | None = None):
        self._content = content
        resolved = abs_path if abs_path is not None else Path(f"/fake/{transcript_id}.jsonl")
        self.file_path = SimpleNamespace(
            path=PurePosixPath(f"{transcript_id}.jsonl"),
            resolve=lambda: resolved,
        )

    async def read_text(self) -> str:
        return self._content


def _run(coro):
    return asyncio.run(coro)


class TestProcessTranscriptEarlyReturns:
    def test_empty_content_returns_without_extracting_windows(self, cocoindex_flows, monkeypatch):
        def fail_if_called(messages):
            raise AssertionError("_extract_learning_windows should not run on empty content")

        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", fail_if_called)
        _run(cocoindex_flows.process_transcript(FakeFile("")))  # must not raise

    def test_whitespace_only_content_returns_early(self, cocoindex_flows, monkeypatch):
        def fail_if_called(messages):
            raise AssertionError("_extract_learning_windows should not run on whitespace-only content")

        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", fail_if_called)
        _run(cocoindex_flows.process_transcript(FakeFile("   \n  \n")))

    def test_no_parseable_json_lines_returns_early(self, cocoindex_flows, monkeypatch):
        def fail_if_called(messages):
            raise AssertionError("_extract_learning_windows should not run with zero parsed messages")

        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", fail_if_called)
        _run(cocoindex_flows.process_transcript(FakeFile("not json\nalso not json")))

    def test_malformed_lines_are_skipped_but_valid_ones_still_parsed(self, cocoindex_flows, monkeypatch):
        """A mix of malformed and well-formed-but-signal-free lines must not
        crash, and must not trigger any correction/instruction classification
        (no recognizable user/assistant role -> zero parsed entries)."""
        content = "not json\n" + json.dumps({"role": "system", "message": {"content": "hello"}})
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [])
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))
        _run(cocoindex_flows.process_transcript(FakeFile(content)))
        assert retain_calls == []


class TestProcessTranscriptContradictionBranching:
    def test_non_correction_window_skips_contradiction_check(self, cocoindex_flows, monkeypatch):
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [
            "[INSTRUCTION] User: always write tests first",
        ])
        resolve_calls = []
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: resolve_calls.append(a) or cr.Resolution(action="retain"))
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(cocoindex_flows.process_transcript(FakeFile('{"role": "user"}', "tid-1")))

        assert resolve_calls == []
        assert len(retain_calls) == 1
        assert retain_calls[0]["tags"] is None

    def test_correction_window_retain_action_calls_hindsight_retain_without_tags(self, cocoindex_flows, monkeypatch):
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(action="retain"))
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(cocoindex_flows.process_transcript(FakeFile('{"role": "user"}', "tid-1")))

        assert len(retain_calls) == 1
        assert retain_calls[0]["tags"] is None
        assert retain_calls[0]["document_id"] == "transcript-tid-1-w0"
        assert retain_calls[0]["metadata"]["transcript_id"] == "tid-1"

    def test_correction_window_auto_resolved_calls_hindsight_retain_with_supersedes_tag(self, cocoindex_flows, monkeypatch):
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(
            action="auto_resolved", superseded_document_id="old-doc", confidence=0.95,
        ))
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(cocoindex_flows.process_transcript(FakeFile('{"role": "user"}', "tid-1")))

        assert len(retain_calls) == 1
        assert retain_calls[0]["tags"] == ["CORRECTION", "supersedes-prior-memory"]

    def test_regression_correction_window_queued_action_skips_hindsight_retain(self, cocoindex_flows, monkeypatch):
        """Guards the 2026-07-13 bug: queued items must NOT be retained --
        they are withheld pending human review in review-contradictions.py."""
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(
            action="queued", superseded_document_id="old-doc", confidence=0.5,
        ))
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(cocoindex_flows.process_transcript(FakeFile('{"role": "user"}', "tid-1")))

        assert retain_calls == [], "hindsight_retain must not be called for a queued resolution"

    def test_mixed_windows_only_retains_non_queued(self, cocoindex_flows, monkeypatch):
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [
            "[CORRECTION] User: statement A",
            "[CORRECTION] User: statement B",
            "[CORRECTION] User: statement C",
        ])
        actions = iter(["retain", "queued", "auto_resolved"])
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(action=next(actions), superseded_document_id="old-doc"))
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(cocoindex_flows.process_transcript(FakeFile('{"role": "user"}', "tid-1")))

        assert len(retain_calls) == 2
        assert retain_calls[0]["document_id"] == "transcript-tid-1-w0"
        assert retain_calls[1]["document_id"] == "transcript-tid-1-w2"

    def test_blank_window_is_skipped_entirely(self, cocoindex_flows, monkeypatch):
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [
            "   ",
            "[CORRECTION] User: real content",
        ])
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(action="retain"))
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(cocoindex_flows.process_transcript(FakeFile('{"role": "user"}', "tid-1")))

        assert len(retain_calls) == 1
        assert retain_calls[0]["document_id"] == "transcript-tid-1-w1"


class TestProcessTranscriptProjectTagging:
    """Regression coverage for the 2026-07-19 fix: contradiction queue
    entries had project=null because process_transcript() never resolved the
    transcript's workspace directory to an onboarded project. See
    docs/FINDINGS.md."""

    def _file_under(self, transcripts_root: Path, project_dir_name: str, transcript_id: str = "tid-1") -> FakeFile:
        abs_path = transcripts_root / project_dir_name / "agent-transcripts" / f"{transcript_id}.jsonl"
        return FakeFile('{"role": "user"}', transcript_id, abs_path=abs_path)

    def test_kubernaut_transcript_resolves_to_kubernaut_project(self, cocoindex_flows, monkeypatch, tmp_path):
        monkeypatch.setattr(cocoindex_flows, "ENGRAM_TRANSCRIPTS_DIR", tmp_path)
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        resolve_calls = []
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: resolve_calls.append(k.get("project")) or cr.Resolution(action="retain"))
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: None)

        file = self._file_under(tmp_path, "Users-jgil-go-src-github-com-jordigilh-kubernaut")
        _run(cocoindex_flows.process_transcript(file))

        assert resolve_calls == ["kubernaut"]

    def test_out_of_scope_transcript_resolves_to_none_project(self, cocoindex_flows, monkeypatch, tmp_path):
        monkeypatch.setattr(cocoindex_flows, "ENGRAM_TRANSCRIPTS_DIR", tmp_path)
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        resolve_calls = []
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: resolve_calls.append(k.get("project")) or cr.Resolution(action="retain"))
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: None)

        file = self._file_under(tmp_path, "Users-jgil-go-src-github-com-insights-onprem-koku")
        _run(cocoindex_flows.process_transcript(file))

        assert resolve_calls == [None]

    def test_path_outside_transcripts_dir_resolves_to_none_project(self, cocoindex_flows, monkeypatch, tmp_path):
        monkeypatch.setattr(cocoindex_flows, "ENGRAM_TRANSCRIPTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        resolve_calls = []
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: resolve_calls.append(k.get("project")) or cr.Resolution(action="retain"))
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: None)

        file = self._file_under(tmp_path / "elsewhere", "Users-jgil-go-src-github-com-jordigilh-kubernaut")
        _run(cocoindex_flows.process_transcript(file))

        assert resolve_calls == [None]


class TestHindsightRetain:
    def test_success_returns_parsed_json(self, cocoindex_flows, monkeypatch):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"success": True}).encode()

        monkeypatch.setattr(cocoindex_flows, "urlopen", lambda req, timeout=60: FakeResponse())
        result = cocoindex_flows.hindsight_retain(bank_id="cursor-memory", content="x", document_id="doc-1")
        assert result == {"success": True}

    def test_retries_then_gives_up_returning_empty_dict(self, cocoindex_flows, monkeypatch):
        from urllib.error import URLError

        def always_fails(req, timeout=60):
            raise URLError("connection refused")

        monkeypatch.setattr(cocoindex_flows, "urlopen", always_fails)
        monkeypatch.setattr(cocoindex_flows.time, "sleep", lambda *_: None)

        result = cocoindex_flows.hindsight_retain(bank_id="cursor-memory", content="x", document_id="doc-1")
        assert result == {}
