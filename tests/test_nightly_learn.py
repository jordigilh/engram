"""Tests for nightly-learn.py's core retain logic: the three-tier
contradiction branching in retain_windows() (regression for the 2026-07-13
bug where "queued" items were retained immediately, before human review),
hash-based dedup in retain_windows_deduped(), dedup_graph()'s newest-wins
duplicate removal, and find_recent_transcripts()'s workspace_prefixes
filtering (regression for the Fix 1b project-scoping change).
"""
from __future__ import annotations

import contradiction_resolution as cr
import project_scope


class TestFindRecentTranscripts:
    def _make_tree(self, tmp_path):
        in_scope = tmp_path / "Users-jgil-go-src-github-com-jordigilh-kubernaut" / "agent-transcripts"
        in_scope.mkdir(parents=True)
        (in_scope / "t1.jsonl").write_text("{}")

        out_of_scope = tmp_path / "Users-jgil-go-src-github-com-insights-onprem-koku" / "agent-transcripts"
        out_of_scope.mkdir(parents=True)
        (out_of_scope / "t2.jsonl").write_text("{}")

        return in_scope / "t1.jsonl", out_of_scope / "t2.jsonl"

    def test_regression_workspace_prefixes_filters_out_of_scope_transcripts(self, nightly_learn, tmp_path, monkeypatch):
        t1, t2 = self._make_tree(tmp_path)
        monkeypatch.setattr(nightly_learn, "TRANSCRIPTS_GLOB", str(tmp_path / "*" / "agent-transcripts" / "**" / "*.jsonl"))
        monkeypatch.setattr(nightly_learn, "PROJECTS_ROOT", tmp_path)

        results = nightly_learn.find_recent_transcripts(
            hours=10 ** 9, workspace_prefixes=project_scope.ALLOWED_WORKSPACE_PREFIXES
        )

        assert t1 in results
        assert t2 not in results

    def test_no_workspace_prefixes_returns_everything(self, nightly_learn, tmp_path, monkeypatch):
        """Backward compat: callers that don't pass workspace_prefixes (e.g.
        unscoped historical call sites) still see every transcript."""
        t1, t2 = self._make_tree(tmp_path)
        monkeypatch.setattr(nightly_learn, "TRANSCRIPTS_GLOB", str(tmp_path / "*" / "agent-transcripts" / "**" / "*.jsonl"))
        monkeypatch.setattr(nightly_learn, "PROJECTS_ROOT", tmp_path)

        results = nightly_learn.find_recent_transcripts(hours=10 ** 9, workspace_prefixes=None)

        assert t1 in results
        assert t2 in results

    def test_respects_hours_cutoff(self, nightly_learn, tmp_path, monkeypatch):
        self._make_tree(tmp_path)
        monkeypatch.setattr(nightly_learn, "TRANSCRIPTS_GLOB", str(tmp_path / "*" / "agent-transcripts" / "**" / "*.jsonl"))
        monkeypatch.setattr(nightly_learn, "PROJECTS_ROOT", tmp_path)

        # A negative hours value pushes the cutoff into the far future, which
        # no on-disk file's mtime can satisfy.
        assert nightly_learn.find_recent_transcripts(hours=-10 ** 9) == []


class TestRetainWindows:
    def test_non_correction_window_skips_contradiction_check(self, nightly_learn, monkeypatch):
        calls = []
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: calls.append(a) or cr.Resolution(action="retain"))
        monkeypatch.setattr(nightly_learn, "api_post", lambda *a, **k: {"success": True, "items_count": 1, "usage": {}})

        result = nightly_learn.retain_windows(["[INSTRUCTION] User: always write tests first"], "tid-1")

        assert calls == [], "resolve() must only run for [CORRECTION]-tagged windows"
        assert result["items_retained"] == 1

    def test_correction_window_retain_action_posts_without_tags(self, nightly_learn, monkeypatch):
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(action="retain"))
        posted = []
        monkeypatch.setattr(nightly_learn, "api_post", lambda path, payload: posted.append(payload) or {"success": True, "items_count": 1, "usage": {}})

        result = nightly_learn.retain_windows(["[CORRECTION] User: we don't use HAPI"], "tid-1")

        assert len(posted) == 1
        assert "tags" not in posted[0]["items"][0]
        assert result["items_retained"] == 1
        assert result["contradictions_auto_resolved"] == 0
        assert result["contradictions_queued"] == 0

    def test_correction_window_auto_resolved_action_posts_with_supersedes_tag(self, nightly_learn, monkeypatch):
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(
            action="auto_resolved", superseded_document_id="old-doc", confidence=0.95,
        ))
        posted = []
        monkeypatch.setattr(nightly_learn, "api_post", lambda path, payload: posted.append(payload) or {"success": True, "items_count": 1, "usage": {}})

        result = nightly_learn.retain_windows(["[CORRECTION] User: we don't use HAPI"], "tid-1")

        assert posted[0]["items"][0]["tags"] == ["CORRECTION", "supersedes-prior-memory"]
        assert result["items_retained"] == 1
        assert result["contradictions_auto_resolved"] == 1

    def test_regression_correction_window_queued_action_is_never_retained(self, nightly_learn, monkeypatch):
        """Guards the 2026-07-13 bug: queued items must NOT be retained --
        they are withheld pending human review in review-contradictions.py."""
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(
            action="queued", superseded_document_id="old-doc", confidence=0.5,
        ))
        posted = []
        monkeypatch.setattr(nightly_learn, "api_post", lambda path, payload: posted.append(payload) or {"success": True})

        result = nightly_learn.retain_windows(["[CORRECTION] User: we don't use HAPI"], "tid-1")

        assert posted == [], "api_post must not be called for a queued resolution"
        assert result["items_retained"] == 0
        assert result["contradictions_queued"] == 1

    def test_mixed_batch_only_posts_for_retain_and_auto_resolved(self, nightly_learn, monkeypatch):
        actions = iter(["retain", "queued", "auto_resolved"])

        def fake_resolve(*a, **k):
            action = next(actions)
            return cr.Resolution(action=action, superseded_document_id="old-doc" if action != "retain" else None, confidence=0.5)

        monkeypatch.setattr(cr, "resolve", fake_resolve)
        posted = []
        monkeypatch.setattr(nightly_learn, "api_post", lambda path, payload: posted.append(payload) or {"success": True, "items_count": 1, "usage": {}})

        windows = [
            "[CORRECTION] User: statement A",
            "[CORRECTION] User: statement B",
            "[CORRECTION] User: statement C",
        ]
        result = nightly_learn.retain_windows(windows, "tid-1")

        assert len(posted) == 2, "only retain + auto_resolved should call api_post, queued must be skipped"
        assert result["items_retained"] == 2
        assert result["contradictions_auto_resolved"] == 1
        assert result["contradictions_queued"] == 1

    def test_api_post_exception_is_caught_and_logged(self, nightly_learn, monkeypatch):
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(action="retain"))

        def raise_error(path, payload):
            raise RuntimeError("network down")

        monkeypatch.setattr(nightly_learn, "api_post", raise_error)

        result = nightly_learn.retain_windows(["[CORRECTION] User: we don't use HAPI"], "tid-1")
        assert result["items_retained"] == 0

    def test_api_post_success_false_does_not_increment_items_retained(self, nightly_learn, monkeypatch):
        monkeypatch.setattr(cr, "resolve", lambda *a, **k: cr.Resolution(action="retain"))
        monkeypatch.setattr(nightly_learn, "api_post", lambda path, payload: {"success": False})

        result = nightly_learn.retain_windows(["[CORRECTION] User: we don't use HAPI"], "tid-1")
        assert result["items_retained"] == 0


class TestRetainWindowsDeduped:
    def test_new_windows_are_retained_and_hashed(self, nightly_learn, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(nightly_learn, "retain_windows", lambda windows, tid: retain_calls.append(list(windows)) or {
            "items_retained": len(windows), "usage": {}, "contradictions_auto_resolved": 0, "contradictions_queued": 0,
        })

        seen_hashes = set()
        result = nightly_learn.retain_windows_deduped(["A", "B"], "tid-1", seen_hashes)

        assert retain_calls == [["A", "B"]]
        assert result["items_retained"] == 2
        assert result["skipped_duplicates"] == 0
        assert len(seen_hashes) == 2

    def test_previously_seen_hash_is_skipped_on_second_call(self, nightly_learn, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(nightly_learn, "retain_windows", lambda windows, tid: retain_calls.append(list(windows)) or {
            "items_retained": len(windows), "usage": {}, "contradictions_auto_resolved": 0, "contradictions_queued": 0,
        })

        seen_hashes = set()
        nightly_learn.retain_windows_deduped(["A", "B"], "tid-1", seen_hashes)
        result2 = nightly_learn.retain_windows_deduped(["A", "C"], "tid-1", seen_hashes)

        assert retain_calls[-1] == ["C"], "A should be skipped as already-seen"
        assert result2["skipped_duplicates"] == 1
        assert result2["items_retained"] == 1

    def test_all_duplicates_short_circuits_without_calling_retain_windows(self, nightly_learn, monkeypatch):
        import hashlib

        # Pre-seed the hash directly (mirrors retain_windows_deduped()'s own
        # hashing) rather than priming via a real call, so retain_windows can
        # be mocked to fail loudly for the entire test.
        seen_hashes = {hashlib.sha256(b"A").hexdigest()}

        def fail_if_called(windows, tid):
            raise AssertionError("retain_windows should not be called when everything is a duplicate")

        monkeypatch.setattr(nightly_learn, "retain_windows", fail_if_called)

        result = nightly_learn.retain_windows_deduped(["A"], "tid-1", seen_hashes)

        assert result["items_retained"] == 0
        assert result["skipped_duplicates"] == 1


class TestDedupGraph:
    def test_keeps_newest_document_per_content_hash_and_deletes_the_rest(self, nightly_learn, monkeypatch):
        docs_page = [
            {"id": "old-1", "content_hash": "h1", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "newest-1", "content_hash": "h1", "created_at": "2026-03-01T00:00:00Z"},
            {"id": "mid-1", "content_hash": "h1", "created_at": "2026-02-01T00:00:00Z"},
            {"id": "unique-1", "content_hash": "h2", "created_at": "2026-01-01T00:00:00Z"},
        ]

        call_count = {"n": 0}

        def fake_api_get(path):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"documents": docs_page}
            return {"documents": []}

        monkeypatch.setattr(nightly_learn, "api_get", fake_api_get)

        deleted_ids = []

        def fake_urlopen(req, timeout=10):
            deleted_ids.append(req.full_url.rsplit("/", 1)[-1])
            return None

        monkeypatch.setattr(nightly_learn, "urlopen", fake_urlopen)

        deleted_count = nightly_learn.dedup_graph("cursor-memory")

        assert deleted_count == 2
        assert set(deleted_ids) == {"old-1", "mid-1"}
        assert "newest-1" not in deleted_ids
        assert "unique-1" not in deleted_ids

    def test_no_duplicates_returns_zero_without_deleting(self, nightly_learn, monkeypatch):
        docs_page = [{"id": "a", "content_hash": "h1", "created_at": "2026-01-01T00:00:00Z"}]

        monkeypatch.setattr(nightly_learn, "api_get", lambda path: {"documents": docs_page})

        def fail_if_called(req, timeout=10):
            raise AssertionError("urlopen should not be called when there are no duplicates")

        monkeypatch.setattr(nightly_learn, "urlopen", fail_if_called)

        assert nightly_learn.dedup_graph("cursor-memory") == 0
