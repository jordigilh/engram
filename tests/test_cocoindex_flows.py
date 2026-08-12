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

import pytest

from engram import contradiction_resolution as cr


@pytest.fixture(autouse=True)
def _isolate_transcript_watermarks(cocoindex_flows, tmp_path, monkeypatch):
    """Every test gets its own watermark file, never the real
    ~/.hindsight/logs/cocoindex-transcript-watermarks.json -- the
    cocoindex_flows module fixture is session-scoped, so without this,
    watermark state would both leak into the real state file and bleed
    across unrelated tests within the session. See docs/FINDINGS.md
    2026-07-31."""
    monkeypatch.setattr(cocoindex_flows, "TRANSCRIPT_WATERMARKS_PATH", tmp_path / "watermarks.json")


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
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [])
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))
        _run(cocoindex_flows.process_transcript(FakeFile(content)))
        assert retain_calls == []


class TestProcessTranscriptContradictionBranching:
    def test_non_correction_window_skips_contradiction_check(self, cocoindex_flows, monkeypatch):
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
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
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(action="retain"))
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(cocoindex_flows.process_transcript(FakeFile('{"role": "user"}', "tid-1")))

        assert len(retain_calls) == 1
        assert retain_calls[0]["tags"] is None
        assert retain_calls[0]["document_id"] == cocoindex_flows._window_document_id(
            "tid-1", "[CORRECTION] User: we don't use HAPI"
        )
        assert retain_calls[0]["metadata"]["transcript_id"] == "tid-1"

    def test_correction_window_auto_resolved_calls_hindsight_retain_with_supersedes_tag(self, cocoindex_flows, monkeypatch):
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
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
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
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
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
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
        assert retain_calls[0]["document_id"] == cocoindex_flows._window_document_id(
            "tid-1", "[CORRECTION] User: statement A"
        )
        assert retain_calls[1]["document_id"] == cocoindex_flows._window_document_id(
            "tid-1", "[CORRECTION] User: statement C"
        )

    def test_blank_window_is_skipped_entirely(self, cocoindex_flows, monkeypatch):
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
            "   ",
            "[CORRECTION] User: real content",
        ])
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(action="retain"))
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(cocoindex_flows.process_transcript(FakeFile('{"role": "user"}', "tid-1")))

        assert len(retain_calls) == 1
        assert retain_calls[0]["document_id"] == cocoindex_flows._window_document_id(
            "tid-1", "[CORRECTION] User: real content"
        )


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
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
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
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        resolve_calls = []
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: resolve_calls.append(k.get("project")) or cr.Resolution(action="retain"))
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: None)

        file = self._file_under(tmp_path, "Users-jgil-go-src-github-com-someorg-unrelated-repo")
        _run(cocoindex_flows.process_transcript(file))

        assert resolve_calls == [None]

    def test_path_outside_transcripts_dir_resolves_to_none_project(self, cocoindex_flows, monkeypatch, tmp_path):
        monkeypatch.setattr(cocoindex_flows, "ENGRAM_TRANSCRIPTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        resolve_calls = []
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: resolve_calls.append(k.get("project")) or cr.Resolution(action="retain"))
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: None)

        file = self._file_under(tmp_path / "elsewhere", "Users-jgil-go-src-github-com-jordigilh-kubernaut")
        _run(cocoindex_flows.process_transcript(file))

        assert resolve_calls == [None]

    def test_project_tag_is_written_onto_retained_item(self, cocoindex_flows, monkeypatch, tmp_path):
        """Regression for the 2026-07-27 gap: retain_windows() in
        nightly-learn.py got tagged with [project] the same day this file's
        parallel process_transcript() retain call did not, so cocoindex-path
        transcripts (e.g. same-day dcm-project/osac-service-provider,
        kubernaut-v1-5 work) kept landing in cursor-memory untagged even
        though the workspace resolved cleanly. See docs/FINDINGS.md."""
        monkeypatch.setattr(cocoindex_flows, "ENGRAM_TRANSCRIPTS_DIR", tmp_path)
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
            "[INSTRUCTION] User: always write tests first",
        ])
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        file = self._file_under(tmp_path, "Users-jgil-go-src-github-com-dcm-project-osac-service-provider")
        _run(cocoindex_flows.process_transcript(file))

        assert retain_calls[0]["tags"] == ["dcm"]

    def test_no_project_means_no_tags_backward_compat(self, cocoindex_flows, monkeypatch, tmp_path):
        monkeypatch.setattr(cocoindex_flows, "ENGRAM_TRANSCRIPTS_DIR", tmp_path)
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
            "[INSTRUCTION] User: always write tests first",
        ])
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        file = self._file_under(tmp_path, "Users-jgil-go-src-github-com-someorg-unrelated-repo")
        _run(cocoindex_flows.process_transcript(file))

        assert retain_calls[0]["tags"] is None

    def test_project_tag_combined_with_supersedes_tag_on_auto_resolved(self, cocoindex_flows, monkeypatch, tmp_path):
        monkeypatch.setattr(cocoindex_flows, "ENGRAM_TRANSCRIPTS_DIR", tmp_path)
        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", lambda messages, start_index=0: [
            "[CORRECTION] User: we don't use HAPI",
        ])
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(
            action="auto_resolved", superseded_document_id="old-doc", confidence=0.95,
        ))
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        file = self._file_under(tmp_path, "Users-jgil-go-src-github-com-jordigilh-kubernaut")
        _run(cocoindex_flows.process_transcript(file))

        assert retain_calls[0]["tags"] == ["kubernaut", "CORRECTION", "supersedes-prior-memory"]


class TestExtractLearningWindowsStartIndex:
    """Unit coverage for _extract_learning_windows()'s start_index filter --
    the piece that lets process_transcript() only emit windows for NEW
    signals while still drawing correct before-context from older messages.
    See docs/FINDINGS.md 2026-07-31."""

    def _messages(self, n: int) -> list[dict]:
        return [{"role": "user", "text": f"msg{i}"} for i in range(n)]

    def _patch_classifiers(self, cocoindex_flows, monkeypatch, correction_at: set[int]):
        monkeypatch.setattr(cocoindex_flows, "_extract_user_text", lambda m: m["text"])
        monkeypatch.setattr(cocoindex_flows, "_extract_assistant_text", lambda m: m["text"])
        monkeypatch.setattr(
            cocoindex_flows, "_is_correction",
            lambda text: int(text.replace("msg", "")) in correction_at,
        )
        monkeypatch.setattr(cocoindex_flows, "_is_instruction", lambda text: False)

    def test_start_index_zero_returns_all_windows(self, cocoindex_flows, monkeypatch):
        self._patch_classifiers(cocoindex_flows, monkeypatch, correction_at={1, 3})
        windows = cocoindex_flows._extract_learning_windows(self._messages(5), start_index=0)
        assert len(windows) == 2

    def test_signal_before_start_index_is_not_emitted(self, cocoindex_flows, monkeypatch):
        """The exact watermark scenario: msg1 was already scanned (and
        retained/queued) on a prior invocation; only msg3 is new."""
        self._patch_classifiers(cocoindex_flows, monkeypatch, correction_at={1, 3})
        windows = cocoindex_flows._extract_learning_windows(self._messages(5), start_index=2)
        assert len(windows) == 1
        assert "[CORRECTION] User: msg3" in windows[0]

    def test_new_signal_near_boundary_still_gets_before_context_from_old_messages(self, cocoindex_flows, monkeypatch):
        """A new signal right at start_index must still pull its `window`
        messages of context from before the watermark cutoff -- the
        watermark only gates which messages count as *signals*, not what
        context surrounding messages are visible."""
        self._patch_classifiers(cocoindex_flows, monkeypatch, correction_at={2})
        windows = cocoindex_flows._extract_learning_windows(self._messages(5), window=2, start_index=2)
        assert len(windows) == 1
        assert "msg0" in windows[0]
        assert "msg1" in windows[0]
        assert "[CORRECTION] User: msg2" in windows[0]

    def test_no_new_signals_past_start_index_returns_empty(self, cocoindex_flows, monkeypatch):
        self._patch_classifiers(cocoindex_flows, monkeypatch, correction_at={1})
        windows = cocoindex_flows._extract_learning_windows(self._messages(5), start_index=2)
        assert windows == []


class TestProcessTranscriptWatermarking:
    """Regression coverage for the 2026-07-31 fix: process_transcript() must
    only extract/re-check messages past what it has already scanned for a
    given transcript_id, instead of re-scanning the whole file (and
    re-running recall()/contradiction checks) on every live file-change
    event. See docs/FINDINGS.md."""

    def _jsonl(self, n: int) -> str:
        return "\n".join(json.dumps({"role": "user", "message": {"content": f"msg{i}"}}) for i in range(n))

    def _capture_start_index(self, cocoindex_flows, monkeypatch, windows_to_return=None):
        calls = []

        def fake_extract(messages, window=2, start_index=0):
            calls.append(start_index)
            return windows_to_return or []

        monkeypatch.setattr(cocoindex_flows, "_extract_learning_windows", fake_extract)
        return calls

    def test_first_call_on_unseen_transcript_uses_start_index_zero(self, cocoindex_flows, monkeypatch):
        calls = self._capture_start_index(cocoindex_flows, monkeypatch)
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(3), "tid-1")))
        assert calls == [0]

    def test_watermark_is_persisted_after_processing(self, cocoindex_flows, monkeypatch):
        self._capture_start_index(cocoindex_flows, monkeypatch)
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(3), "tid-1")))

        watermarks = cocoindex_flows._load_transcript_watermarks()
        assert watermarks["tid-1"]["message_count"] == 3

    def test_regression_second_call_on_grown_transcript_only_scans_new_tail(self, cocoindex_flows, monkeypatch):
        """The exact production bug: a live session grows by a few messages
        and the file-watcher re-delivers the FULL file. Without the
        watermark, start_index would be 0 again on every call."""
        calls = self._capture_start_index(cocoindex_flows, monkeypatch)
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(3), "tid-1")))
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(7), "tid-1")))

        assert calls == [0, 3]

    def test_unchanged_content_reprocessed_still_uses_watermarked_start_index(self, cocoindex_flows, monkeypatch):
        """CocoIndex's memo=True should normally prevent a re-invocation on
        truly unchanged content, but if it ever fires anyway (or another
        watcher path re-triggers it), the watermark must still hold: no new
        messages means start_index == full message count, so
        _extract_learning_windows() finds nothing new to emit."""
        calls = self._capture_start_index(cocoindex_flows, monkeypatch)
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(3), "tid-1")))
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(3), "tid-1")))

        assert calls == [0, 3]

    def test_different_transcripts_track_independent_watermarks(self, cocoindex_flows, monkeypatch):
        calls = self._capture_start_index(cocoindex_flows, monkeypatch)
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(5), "tid-a")))
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(2), "tid-b")))
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(9), "tid-a")))

        assert calls == [0, 0, 5]

    def test_regression_shrunk_transcript_clamps_start_index_to_zero_instead_of_erroring(self, cocoindex_flows, monkeypatch):
        """Defends against a rewritten/truncated transcript file producing a
        stale watermark greater than the new message count -- must reset
        to 0 rather than pass an out-of-range start_index."""
        calls = self._capture_start_index(cocoindex_flows, monkeypatch)
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(8), "tid-1")))
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(2), "tid-1")))

        assert calls == [0, 0]

    def test_new_windows_get_stable_content_hashed_document_ids_not_reused_positional_ones(
        self, cocoindex_flows, monkeypatch,
    ):
        """Regression: before this fix, document_id was `-w{enumerate index}`
        into the (now watermark-sliced) windows list, so a second call's
        first NEW window would reuse `-w0` and silently overwrite an
        unrelated, already-retained window from the first call."""
        from engram import contradiction_resolution as cr_module
        monkeypatch.setattr(cr_module, "resolve", lambda *a, **k: cr_module.Resolution(action="retain"))

        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        windows_by_call = iter([
            ["[CORRECTION] User: first correction"],
            ["[CORRECTION] User: second correction"],
        ])
        monkeypatch.setattr(
            cocoindex_flows, "_extract_learning_windows",
            lambda messages, window=2, start_index=0: next(windows_by_call),
        )

        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(3), "tid-1")))
        _run(cocoindex_flows.process_transcript(FakeFile(self._jsonl(6), "tid-1")))

        assert len(retain_calls) == 2
        assert retain_calls[0]["document_id"] != retain_calls[1]["document_id"]


class TestFormatIssueHeader:
    """Regression coverage for the 2026-08-03 fix: state/labels must not
    appear in the chunked header text, since they change routinely (PR
    merges, triage) and would otherwise cascade every comment chunk's
    stored content-hash on every such change. See docs/FINDINGS.md."""

    def _issue(self, **overrides):
        issue = {
            "number": 42,
            "title": "Something broke",
            "state": "OPEN",
            "_kind": "issue",
            "labels": [{"name": "bug"}],
            "author": {"login": "alice"},
            "createdAt": "2026-01-01T00:00:00Z",
            "body": "Full description here.",
        }
        issue.update(overrides)
        return issue

    def test_header_excludes_state_and_labels(self, cocoindex_flows):
        header = cocoindex_flows._format_issue_header(self._issue(), "jordigilh/kubernaut")
        assert "OPEN" not in header
        assert "bug" not in header
        assert "State" not in header
        assert "Labels" not in header

    def test_header_includes_stable_fields(self, cocoindex_flows):
        header = cocoindex_flows._format_issue_header(self._issue(), "jordigilh/kubernaut")
        assert "Something broke" in header
        assert "alice" in header
        assert "2026-01-01" in header
        assert "Full description here." in header

    def test_regression_state_change_does_not_change_header_text(self, cocoindex_flows):
        """The exact cascade trigger: an issue/PR's state flips (e.g. merged)
        with nothing else changing. Header text must be byte-identical."""
        open_header = cocoindex_flows._format_issue_header(self._issue(state="OPEN"), "jordigilh/kubernaut")
        closed_header = cocoindex_flows._format_issue_header(self._issue(state="CLOSED"), "jordigilh/kubernaut")
        assert open_header == closed_header

    def test_regression_label_change_does_not_change_header_text(self, cocoindex_flows):
        no_labels = cocoindex_flows._format_issue_header(self._issue(labels=[]), "jordigilh/kubernaut")
        with_labels = cocoindex_flows._format_issue_header(
            self._issue(labels=[{"name": "bug"}, {"name": "p1"}]), "jordigilh/kubernaut",
        )
        assert no_labels == with_labels


class TestProcessIssue:
    def _issue(self, **overrides):
        issue = {
            "number": 42,
            "title": "Something broke",
            "state": "OPEN",
            "_kind": "issue",
            "labels": [{"name": "bug"}],
            "author": {"login": "alice"},
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "body": "Full description here.",
            "comments": [],
        }
        issue.update(overrides)
        return issue

    def _comment(self, author="bob", body=None, association="MEMBER"):
        return {
            "author": {"login": author},
            "body": body or ("word " * 10),
            "authorAssociation": association,
        }

    def test_header_only_issue_produces_single_bare_document_id(self, cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        cocoindex_flows.process_issue(self._issue(), "jordigilh/kubernaut")

        assert len(retain_calls) == 1
        assert retain_calls[0]["document_id"] == "kubernaut-issue-42"
        assert retain_calls[0]["bank_id"] == "kubernaut-issues"

    def test_comments_get_ordinal_suffixed_document_ids(self, cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        cocoindex_flows.process_issue(
            self._issue(comments=[self._comment(), self._comment()]), "jordigilh/kubernaut",
        )

        doc_ids = [c["document_id"] for c in retain_calls]
        assert doc_ids == ["kubernaut-issue-42", "kubernaut-issue-42-comment0", "kubernaut-issue-42-comment1"]

    def test_regression_new_comment_does_not_change_earlier_comment_document_ids_or_content(
        self, cocoindex_flows, monkeypatch,
    ):
        """The exact kubernaut-issues cascade this fix targets: polling picks
        up a brand-new comment on an existing issue. The header and every
        earlier comment's document_id + content must be unaffected."""
        first_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: first_calls.append(kwargs))
        cocoindex_flows.process_issue(
            self._issue(comments=[self._comment(body="the first comment body text")]), "jordigilh/kubernaut",
        )

        second_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: second_calls.append(kwargs))
        cocoindex_flows.process_issue(
            self._issue(comments=[
                self._comment(body="the first comment body text"),
                self._comment(body="the second comment body text"),
            ]),
            "jordigilh/kubernaut",
        )

        first_by_id = {c["document_id"]: c["content"] for c in first_calls}
        second_by_id = {c["document_id"]: c["content"] for c in second_calls}
        for doc_id, content in first_by_id.items():
            assert second_by_id[doc_id] == content
        assert "kubernaut-issue-42-comment1" in second_by_id
        assert "kubernaut-issue-42-comment1" not in first_by_id

    def test_regression_state_change_does_not_change_header_document_content(self, cocoindex_flows, monkeypatch):
        open_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: open_calls.append(kwargs))
        cocoindex_flows.process_issue(self._issue(state="OPEN"), "jordigilh/kubernaut")

        closed_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: closed_calls.append(kwargs))
        cocoindex_flows.process_issue(self._issue(state="CLOSED"), "jordigilh/kubernaut")

        assert open_calls[0]["document_id"] == closed_calls[0]["document_id"]
        assert open_calls[0]["content"] == closed_calls[0]["content"]
        assert open_calls[0]["metadata"]["state"] == "open"
        assert closed_calls[0]["metadata"]["state"] == "closed"

    def test_bot_and_short_comments_are_filtered_before_chunking(self, cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        cocoindex_flows.process_issue(
            self._issue(comments=[
                self._comment(author="renovate[bot]"),
                self._comment(body="hi"),  # too short (<20 chars)
                self._comment(association="NONE"),
                self._comment(body="a real substantive comment"),
            ]),
            "jordigilh/kubernaut",
        )

        doc_ids = [c["document_id"] for c in retain_calls]
        assert doc_ids == ["kubernaut-issue-42", "kubernaut-issue-42-comment0"]


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

    def test_regression_payload_does_not_include_dead_strategy_field(self, cocoindex_flows, monkeypatch):
        """strategy='exact' was never registered in any bank's
        retain_strategies config, so hindsight-api silently ignored it --
        pure log noise, no behavior. Removed rather than fixed since the
        bank's default delta-retain already skips reprocessing unchanged
        content with no strategy needed. See docs/FINDINGS.md 2026-08-03."""
        captured_requests = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"success": True}).encode()

        def fake_urlopen(req, timeout=60):
            captured_requests.append(json.loads(req.data))
            return FakeResponse()

        monkeypatch.setattr(cocoindex_flows, "urlopen", fake_urlopen)
        cocoindex_flows.hindsight_retain(bank_id="cursor-memory", content="x", document_id="doc-1")

        assert "strategy" not in captured_requests[0]["items"][0]

    def test_retries_then_gives_up_returning_empty_dict(self, cocoindex_flows, monkeypatch):
        from urllib.error import URLError

        def always_fails(req, timeout=60):
            raise URLError("connection refused")

        monkeypatch.setattr(cocoindex_flows, "urlopen", always_fails)
        monkeypatch.setattr(cocoindex_flows.time, "sleep", lambda *_: None)

        result = cocoindex_flows.hindsight_retain(bank_id="cursor-memory", content="x", document_id="doc-1")
        assert result == {}


class TestReleaseLineWiring:
    """code_main's multi-branch extension (2026-08-10): the kubernaut
    family's *code* index additionally covers release/v1.5 and release/v1.6
    mirrors (docs/issues stay main-only -- unchanged, LLM-consolidation-costed
    via hindsight_retain(), unlike code embedding). See
    watch-mirrors-config.sh's RELEASE_LINES/RELEASE_WATCH_MIRRORS and
    docs/FINDINGS.md."""

    def test_default_release_lines_are_v1_5_and_v1_6(self, cocoindex_flows):
        assert cocoindex_flows.KUBERNAUT_RELEASE_LINES == ["v1.5", "v1.6"]

    def test_release_line_dir_matches_watch_mirrors_config_convention(self, cocoindex_flows):
        """Must match watch-mirrors-config.sh's RELEASE_WATCH_MIRRORS
        mirror_path convention exactly (`~/.hindsight/watch/<repo>-release-<line>`)
        -- this script never creates the mirrors itself, it only reads
        wherever watch-mirrors-lib.sh already put them."""
        path = cocoindex_flows._release_line_dir("kubernaut", "v1.5")
        assert str(path).endswith("/.hindsight/watch/kubernaut-release-v1.5")

        path = cocoindex_flows._release_line_dir("kubernaut-operator", "v1.6")
        assert str(path).endswith("/.hindsight/watch/kubernaut-operator-release-v1.6")

    def test_release_line_dir_expands_user_home(self, cocoindex_flows):
        import os

        path = cocoindex_flows._release_line_dir("kubernaut-console", "v1.5")
        assert "~" not in str(path)
        assert str(path).startswith(os.path.expanduser("~"))

    def test_code_main_source_covers_all_three_repos_x_release_lines(self, cocoindex_flows):
        """Structural regression (mirrors test_koku_cocoindex_flows.py's
        PR_REPOS-membership style): asserting on code_main's *source*
        rather than executing it, since a real run needs a live postgres
        pool + coco flow-graph runtime that isn't worth faking out here.
        Confirms the release-line loop is wired for all 3 repos with the
        exact repo_tag format code relies on downstream (cocoindex-search.py's
        branch filtering matches on the literal "@release-" substring)."""
        import inspect

        source = inspect.getsource(cocoindex_flows.code_main)
        assert "KUBERNAUT_RELEASE_LINES" in source
        assert "_release_line_dir" in source
        assert '@release-{line}"' in source
        for repo_name in ("kubernaut", "kubernaut-operator", "kubernaut-console"):
            assert f'"{repo_name}"' in source, f"code_main's release-line loop is missing {repo_name}"
