"""Tests for review-contradictions.py's interactive approve/reject/skip/quit
loop. Regression coverage for the 2026-07-12 bug: the approve path only
tagged the new memory with `supersedes` metadata but never actually deleted
the old conflicting memory.

2026-07-29 (GitHub issue #1): approve now prefers invalidate_memory() (a
non-destructive, reversible PATCH .../memories/{memory_id}) over
delete_document() when the pending entry has a memory_id -- the same
supersede-not-destroy fix applied to contradiction_resolution.py's
auto-resolve path, extended here so the human-reviewed path stops being the
*more* destructive of the two. Entries queued before this fix have no
memory_id and fall back to the legacy delete_document(document_id) path.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def rc(review_contradictions):
    return review_contradictions


def _entry(**overrides):
    base = {
        "id": "pending-1",
        "timestamp": "2026-07-13T00:00:00Z",
        "project": "kubernaut",
        "new_statement": "we don't use HAPI, it's deprecated",
        "conflicting_memory": "we use HAPI for all API calls",
        "conflicting_memory_index": 0,
        "explanation": "direct contradiction",
        "memory_id": "mem-old-1",
        "document_id": "old-doc-1",
    }
    base.update(overrides)
    return base


def _feed_inputs(monkeypatch, *choices):
    it = iter(choices)
    monkeypatch.setattr("builtins.input", lambda *_: next(it))


class TestApprove:
    def test_approve_invalidates_conflicting_memory_and_retains_new_statement(self, rc, monkeypatch):
        entry = _entry()
        monkeypatch.setattr(rc, "load_pending", lambda: [entry])
        monkeypatch.setattr(rc, "_HAS_RETAIN", True)

        invalidate_calls = []
        monkeypatch.setattr(
            rc, "invalidate_memory",
            lambda bank, memory_id, reason=None: invalidate_calls.append((bank, memory_id, reason)) or True,
        )

        def fail_if_called(*a, **k):
            raise AssertionError("delete_document should not be called when memory_id is present")

        monkeypatch.setattr(rc, "delete_document", fail_if_called)

        retain_calls = []
        monkeypatch.setattr(rc._cf, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs) or {"id": "new-doc"})

        remove_calls = []
        monkeypatch.setattr(rc, "remove_pending", lambda entry_id: remove_calls.append(entry_id))

        _feed_inputs(monkeypatch, "a")

        rc.main()

        assert len(invalidate_calls) == 1
        bank, memory_id, reason = invalidate_calls[0]
        assert (bank, memory_id) == ("cursor-memory", "mem-old-1")
        assert reason  # non-empty audit reason recorded
        assert len(retain_calls) == 1
        assert retain_calls[0]["document_id"] == "contradiction-resolved-pending-1"
        assert retain_calls[0]["metadata"]["supersedes_document_id"] == "old-doc-1"
        assert "supersedes-prior-memory" in retain_calls[0]["tags"]
        assert remove_calls == ["pending-1"]

    def test_approve_without_memory_id_falls_back_to_legacy_delete_document(self, rc, monkeypatch):
        """Entries queued before the invalidate fix (issue #1) have no
        memory_id -- fall back to the pre-existing document-level delete
        rather than silently leaving the old (wrong) memory in place."""
        entry = _entry()
        del entry["memory_id"]
        monkeypatch.setattr(rc, "load_pending", lambda: [entry])
        monkeypatch.setattr(rc, "_HAS_RETAIN", True)

        def fail_if_called(*a, **k):
            raise AssertionError("invalidate_memory should not be called with no memory_id")

        monkeypatch.setattr(rc, "invalidate_memory", fail_if_called)

        delete_calls = []
        monkeypatch.setattr(rc, "delete_document", lambda bank, doc_id: delete_calls.append((bank, doc_id)) or True)

        retain_calls = []
        monkeypatch.setattr(rc._cf, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs) or {"id": "new-doc"})
        monkeypatch.setattr(rc, "remove_pending", lambda entry_id: None)

        _feed_inputs(monkeypatch, "a")
        rc.main()

        assert delete_calls == [("cursor-memory", "old-doc-1")]
        assert len(retain_calls) == 1

    def test_approve_without_memory_id_or_document_id_skips_both_but_still_retains(self, rc, monkeypatch):
        """Entries queued before either supersede fix -- conservative
        fallback: leave old memory in place, still retain new."""
        entry = _entry()
        del entry["memory_id"]
        del entry["document_id"]
        monkeypatch.setattr(rc, "load_pending", lambda: [entry])
        monkeypatch.setattr(rc, "_HAS_RETAIN", True)

        def fail_if_called(*a, **k):
            raise AssertionError("neither invalidate_memory nor delete_document should be called")

        monkeypatch.setattr(rc, "invalidate_memory", fail_if_called)
        monkeypatch.setattr(rc, "delete_document", fail_if_called)

        retain_calls = []
        monkeypatch.setattr(rc._cf, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs) or {"id": "new-doc"})
        monkeypatch.setattr(rc, "remove_pending", lambda entry_id: None)

        _feed_inputs(monkeypatch, "a")
        rc.main()

        assert len(retain_calls) == 1
        assert retain_calls[0]["metadata"]["supersedes_document_id"] is None

    def test_approve_disabled_when_retain_unavailable(self, rc, monkeypatch):
        entry = _entry()
        monkeypatch.setattr(rc, "load_pending", lambda: [entry])
        monkeypatch.setattr(rc, "_HAS_RETAIN", False)

        def fail_if_called(*a, **k):
            raise AssertionError("invalidate_memory should not be called when retain is unavailable")

        monkeypatch.setattr(rc, "invalidate_memory", fail_if_called)
        remove_calls = []
        monkeypatch.setattr(rc, "remove_pending", lambda entry_id: remove_calls.append(entry_id))

        _feed_inputs(monkeypatch, "a")
        rc.main()

        assert remove_calls == [], "should not remove from queue if approve couldn't complete"


class TestReject:
    def test_reject_only_removes_from_queue_no_retain_or_invalidate_call(self, rc, monkeypatch):
        entry = _entry()
        monkeypatch.setattr(rc, "load_pending", lambda: [entry])
        monkeypatch.setattr(rc, "_HAS_RETAIN", True)

        def fail_if_called(*a, **k):
            raise AssertionError("reject must not call invalidate_memory or hindsight_retain")

        monkeypatch.setattr(rc, "invalidate_memory", fail_if_called)
        monkeypatch.setattr(rc._cf, "hindsight_retain", fail_if_called)

        remove_calls = []
        monkeypatch.setattr(rc, "remove_pending", lambda entry_id: remove_calls.append(entry_id))

        _feed_inputs(monkeypatch, "r")
        rc.main()

        assert remove_calls == ["pending-1"]


class TestSkip:
    def test_skip_leaves_entry_untouched(self, rc, monkeypatch):
        entry = _entry()
        monkeypatch.setattr(rc, "load_pending", lambda: [entry])
        monkeypatch.setattr(rc, "_HAS_RETAIN", True)

        def fail_if_called(*a, **k):
            raise AssertionError("skip must not touch invalidate_memory, hindsight_retain, or remove_pending")

        monkeypatch.setattr(rc, "invalidate_memory", fail_if_called)
        monkeypatch.setattr(rc._cf, "hindsight_retain", fail_if_called)
        monkeypatch.setattr(rc, "remove_pending", fail_if_called)

        _feed_inputs(monkeypatch, "s")
        rc.main()  # must not raise


class TestQuit:
    def test_quit_stops_before_touching_remaining_entries(self, rc, monkeypatch):
        entries = [_entry(id="pending-1"), _entry(id="pending-2")]
        monkeypatch.setattr(rc, "load_pending", lambda: entries)
        monkeypatch.setattr(rc, "_HAS_RETAIN", True)

        def fail_if_called(*a, **k):
            raise AssertionError("quit must not touch invalidate_memory, hindsight_retain, or remove_pending")

        monkeypatch.setattr(rc, "invalidate_memory", fail_if_called)
        monkeypatch.setattr(rc._cf, "hindsight_retain", fail_if_called)
        monkeypatch.setattr(rc, "remove_pending", fail_if_called)

        _feed_inputs(monkeypatch, "q")
        rc.main()  # must not raise, must stop after the first prompt


class TestNoPendingEntries:
    def test_empty_queue_returns_early(self, rc, monkeypatch):
        monkeypatch.setattr(rc, "load_pending", lambda: [])
        assert rc.main() == 0
