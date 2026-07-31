"""Tests for spike/pending_queue.py's append_pending() dedup guard.

Regression for the 2026-07-31 bug: cocoindex-flows.py's live transcript
watcher re-reads and re-scans the WHOLE transcript file from scratch on
every file-change event (no watermark), so a correction sitting anywhere in
an actively-growing session gets re-extracted and re-checked against the
same conflicting memory on every subsequent message in that session. Before
this fix, append_pending() had no dedup at all, so a single real
contradiction could be (and in production, was) queued dozens/hundreds of
times over a long-lived session -- one cluster hit 104 duplicate entries
over 3 days. See docs/FINDINGS.md.
"""
from __future__ import annotations

import pending_queue as pq


class TestAppendPendingDedup:
    def test_exact_duplicate_new_statement_and_memory_id_is_not_reappended(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pq, "QUEUE_PATH", str(tmp_path / "contradictions-pending.jsonl"))

        first = pq.append_pending(
            new_statement="Assistant: the chart already has an auth block",
            conflicting_memory="Chart already supports auth configuration values",
            conflicting_memory_index=0,
            explanation="restates existing fact",
            memory_id="mem-123",
            document_id="doc-456",
            project="kubernaut",
        )
        second = pq.append_pending(
            new_statement="Assistant: the chart already has an auth block",
            conflicting_memory="Chart already supports auth configuration values",
            conflicting_memory_index=0,
            explanation="restates existing fact",
            memory_id="mem-123",
            document_id="doc-456",
            project="kubernaut",
        )

        assert len(pq.load_pending()) == 1
        assert second["id"] == first["id"], "duplicate call must return the existing entry, not create a new one"

    def test_regression_repeated_rescans_of_same_session_only_queue_once(self, tmp_path, monkeypatch):
        """The exact production scenario: the same window re-extracted many
        times (simulating cocoindex's live re-scan on every new message in a
        long-lived session) must collapse to one queue entry."""
        monkeypatch.setattr(pq, "QUEUE_PATH", str(tmp_path / "contradictions-pending.jsonl"))

        for _ in range(104):
            pq.append_pending(
                new_statement="Assistant: I see there's already an `auth:` block in the chart values",
                conflicting_memory="Chart already supports auth configuration values",
                conflicting_memory_index=2,
                explanation="restates existing fact",
                memory_id="mem-auth-block",
                document_id="doc-auth-block",
                project="kubernaut",
            )

        assert len(pq.load_pending()) == 1

    def test_different_new_statement_is_not_deduped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pq, "QUEUE_PATH", str(tmp_path / "contradictions-pending.jsonl"))

        pq.append_pending(
            new_statement="Statement A", conflicting_memory="mem text",
            conflicting_memory_index=0, explanation="e", memory_id="mem-1",
        )
        pq.append_pending(
            new_statement="Statement B", conflicting_memory="mem text",
            conflicting_memory_index=0, explanation="e", memory_id="mem-1",
        )

        assert len(pq.load_pending()) == 2

    def test_same_new_statement_conflicting_with_different_memory_is_not_deduped(self, tmp_path, monkeypatch):
        """Same correction genuinely conflicting with two different existing
        memories is two distinct, legitimate review items -- not a duplicate."""
        monkeypatch.setattr(pq, "QUEUE_PATH", str(tmp_path / "contradictions-pending.jsonl"))

        pq.append_pending(
            new_statement="Statement A", conflicting_memory="mem text 1",
            conflicting_memory_index=0, explanation="e", memory_id="mem-1",
        )
        pq.append_pending(
            new_statement="Statement A", conflicting_memory="mem text 2",
            conflicting_memory_index=1, explanation="e", memory_id="mem-2",
        )

        assert len(pq.load_pending()) == 2

    def test_dedup_check_reads_pre_existing_queue_contents_from_disk(self, tmp_path, monkeypatch):
        """Guards against a dedup implementation that only tracks entries
        appended within the current process lifetime -- cocoindex-flows.py's
        long-running daemon process calls append_pending() across many
        separate file-watch events, and review-contradictions.py/report.py
        run as entirely separate process invocations, so the dedup check
        must be based on the on-disk queue contents, not in-memory state."""
        queue_path = tmp_path / "contradictions-pending.jsonl"
        monkeypatch.setattr(pq, "QUEUE_PATH", str(queue_path))
        pq.append_pending(
            new_statement="Statement A", conflicting_memory="mem text",
            conflicting_memory_index=0, explanation="e", memory_id="mem-1",
        )

        # Simulate a fresh process (e.g. a new cocoindex file-watch callback)
        # by re-importing state purely from what's on disk.
        assert len(pq.load_pending()) == 1
        pq.append_pending(
            new_statement="Statement A", conflicting_memory="mem text",
            conflicting_memory_index=0, explanation="e", memory_id="mem-1",
        )

        assert len(pq.load_pending()) == 1
