"""Tests for nightly-learn.py's core retain logic: the three-tier
contradiction branching in retain_windows() (regression for the 2026-07-13
bug where "queued" items were retained immediately, before human review),
hash-based dedup in retain_windows_deduped(), dedup_graph()'s newest-wins
duplicate removal, find_recent_transcripts()'s workspace_prefixes filtering
(regression for the Fix 1b project-scoping change), and
analyze_mcp_effectiveness()'s context_loading_tokens computation (the
"tokens burned before first productive action" metric surfaced in report.py
and DASHBOARD.md as of 2026-07-14).
"""
from __future__ import annotations

import json

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


class TestProjectForTranscriptPath:
    """Regression coverage for the 2026-07-19 fix: contradiction queue entries
    had project=null because nothing resolved a transcript path back to
    kubernaut/dcm/engram before calling contradiction_resolution.resolve().
    See docs/FINDINGS.md."""

    def test_resolves_kubernaut_transcript(self, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(nightly_learn, "PROJECTS_ROOT", tmp_path)
        path = tmp_path / "Users-jgil-go-src-github-com-jordigilh-kubernaut" / "agent-transcripts" / "t1.jsonl"

        assert nightly_learn.project_for_transcript_path(path) == "kubernaut"

    def test_resolves_dcm_transcript(self, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(nightly_learn, "PROJECTS_ROOT", tmp_path)
        path = tmp_path / "Users-jgil-go-src-github-com-dcm-project-cli" / "agent-transcripts" / "t1.jsonl"

        assert nightly_learn.project_for_transcript_path(path) == "dcm"

    def test_out_of_scope_transcript_resolves_to_none(self, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(nightly_learn, "PROJECTS_ROOT", tmp_path)
        path = tmp_path / "Users-jgil-go-src-github-com-insights-onprem-koku" / "agent-transcripts" / "t1.jsonl"

        assert nightly_learn.project_for_transcript_path(path) is None

    def test_path_outside_projects_root_resolves_to_none(self, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(nightly_learn, "PROJECTS_ROOT", tmp_path / "projects")
        path = tmp_path / "somewhere-else" / "t1.jsonl"

        assert nightly_learn.project_for_transcript_path(path) is None

    def test_retain_windows_forwards_project_to_resolve(self, nightly_learn, monkeypatch):
        resolve_calls = []
        monkeypatch.setattr(
            nightly_learn.contradiction_resolution, "resolve",
            lambda *a, **k: resolve_calls.append(k.get("project")) or cr.Resolution(action="retain"),
        )
        monkeypatch.setattr(nightly_learn, "api_post", lambda *a, **k: {"usage": {}})

        nightly_learn.retain_windows(["[CORRECTION] User: we don't use HAPI"], "tid-1", project="kubernaut")

        assert resolve_calls == ["kubernaut"]

    def test_retain_windows_deduped_forwards_project(self, nightly_learn, monkeypatch):
        captured = {}

        def fake_retain_windows(windows, tid, project=None):
            captured["project"] = project
            return {"items_retained": 0, "usage": {}, "contradictions_auto_resolved": 0, "contradictions_queued": 0}

        monkeypatch.setattr(nightly_learn, "retain_windows", fake_retain_windows)

        nightly_learn.retain_windows_deduped(["A"], "tid-1", set(), project="dcm")

        assert captured["project"] == "dcm"


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
        monkeypatch.setattr(nightly_learn, "retain_windows", lambda windows, tid, project=None: retain_calls.append(list(windows)) or {
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
        monkeypatch.setattr(nightly_learn, "retain_windows", lambda windows, tid, project=None: retain_calls.append(list(windows)) or {
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


class TestContextLoadingTokens:
    """context_loading_tokens (chars before first productive action, ÷4) is
    the metric proposed by the 2026-07-14 "reduce input tokens" review to
    surface in report.py/DASHBOARD.md. These tests pin its computation so a
    future refactor of analyze_mcp_effectiveness()'s message loop can't
    silently change what "before first productive action" means.
    """

    RECALL_TOOL_USE = {"toolName": "recall", "server": "hindsight-docs"}

    def _write_transcript(self, path, recall_input=None):
        """A 6-message transcript: two user messages before any tool use (one
        of them large, for a precisely predictable char count), an assistant
        turn that only calls recall (non-productive), one more user message,
        then a productive (Write) assistant turn, then a trailing user
        message. Everything up to and including the pre-Write user message
        counts as preamble; the Write turn and everything after does not.
        """
        recall_input = recall_input if recall_input is not None else self.RECALL_TOOL_USE
        messages = [
            {"role": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
            {"role": "user", "message": {"role": "user", "content": [{"type": "text", "text": "A" * 20000}]}},
            {"role": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "CallMcpTool", "input": recall_input},
            ]}},
            {"role": "user", "message": {"role": "user", "content": [{"type": "text", "text": "ok"}]}},
            {"role": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Write", "input": {}},
            ]}},
            {"role": "user", "message": {"role": "user", "content": [{"type": "text", "text": "done"}]}},
        ]
        with open(path, "w") as f:
            for m in messages:
                f.write(json.dumps(m) + "\n")
        recall_tool_use_chars = len(json.dumps(recall_input))
        expected_preamble_chars = len("hi") + 20000 + recall_tool_use_chars + len("ok")
        return expected_preamble_chars

    def test_context_loading_tokens_stops_at_first_productive_action(self, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: False)
        path = tmp_path / "t1.jsonl"
        expected_preamble_chars = self._write_transcript(path)

        result = nightly_learn.analyze_mcp_effectiveness([path], report_date=nightly_learn.date(2026, 7, 14))

        rs = result["recall_session_stats"]
        assert rs["sessions"] == 1
        assert rs["avg_context_loading_tokens"] == round(expected_preamble_chars / 4)

    def test_context_loading_tokens_excludes_the_productive_turn_itself(self, nightly_learn, tmp_path, monkeypatch):
        """Regression guard: the Write tool_use turn's own chars must not be
        folded into the preamble, even though it's the very next message
        after the boundary -- otherwise every session's context_loading_tokens
        would silently drift upward as tool inputs grow."""
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: False)
        path = tmp_path / "t2.jsonl"
        self._write_transcript(path)

        result = nightly_learn.analyze_mcp_effectiveness([path], report_date=nightly_learn.date(2026, 7, 14))

        rs = result["recall_session_stats"]
        # A non-trivial (>=20000-char) Write input would move the average if
        # it leaked into the preamble; confirm it stays pinned to the
        # small-preamble expectation regardless of Write's own payload size.
        big_write_path = tmp_path / "t3.jsonl"
        messages = []
        with open(path) as f:
            for line in f:
                messages.append(json.loads(line))
        messages[4]["message"]["content"][0]["input"] = {"padding": "B" * 50000}
        with open(big_write_path, "w") as f:
            for m in messages:
                f.write(json.dumps(m) + "\n")

        result2 = nightly_learn.analyze_mcp_effectiveness([big_write_path], report_date=nightly_learn.date(2026, 7, 14))
        rs2 = result2["recall_session_stats"]
        assert rs2["avg_context_loading_tokens"] == rs["avg_context_loading_tokens"]


class TestRecallBanksPerProject:
    """Regression guard for the 2026-07-15 fix to analyze_mcp_effectiveness():
    RECALL_BANKS/CODE_BANK used to be hardcoded to kubernaut's server names
    ("hindsight", "hindsight-docs", "hindsight-issues", "cocoindex-code"),
    which silently zeroed out banks_recalled (and the with_cocoindex
    exploration-efficiency bucket) for every other project -- DCM's
    "dcm-docs"/"dcm-issues"/"dcm-code" server names never matched, so DCM
    sessions always looked like they never used cocoindex regardless of
    reality. Both now derive from PROJECT_CONFIGS[project]["recall_banks"]/
    ["code_bank"].
    """

    def _write_minimal_transcript(self, path, recall_server):
        messages = [
            {"role": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
            {"role": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "CallMcpTool",
                 "input": {"toolName": "recall", "server": recall_server}},
            ]}},
            {"role": "user", "message": {"role": "user", "content": [{"type": "text", "text": "ok"}]}},
            {"role": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Write", "input": {}},
            ]}},
            {"role": "user", "message": {"role": "user", "content": [{"type": "text", "text": "thanks"}]}},
            {"role": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {}},
            ]}},
        ]
        with open(path, "w") as f:
            for m in messages:
                f.write(json.dumps(m) + "\n")

    def test_regression_dcm_code_bank_is_counted_for_dcm_project(self, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: False)
        path = tmp_path / "dcm-session.jsonl"
        self._write_minimal_transcript(path, recall_server="dcm-code")

        result = nightly_learn.analyze_mcp_effectiveness(
            [path], project="dcm", report_date=nightly_learn.date(2026, 7, 15),
        )

        assert result["exploration_efficiency"]["with_cocoindex"]["sessions"] == 1

    def test_kubernaut_cocoindex_code_bank_still_counted_by_default(self, nightly_learn, tmp_path, monkeypatch):
        """Non-regression: kubernaut's own server name must keep working."""
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: False)
        path = tmp_path / "kubernaut-session.jsonl"
        self._write_minimal_transcript(path, recall_server="cocoindex-code")

        result = nightly_learn.analyze_mcp_effectiveness(
            [path], project="kubernaut", report_date=nightly_learn.date(2026, 7, 15),
        )

        assert result["exploration_efficiency"]["with_cocoindex"]["sessions"] == 1

    def test_dcm_server_name_not_counted_under_kubernaut_project(self, nightly_learn, tmp_path, monkeypatch):
        """Cross-check: a dcm-code recall analyzed under project="kubernaut"
        must NOT count as cocoindex usage -- proves the bank set is genuinely
        derived per-project, not just widened to accept everything."""
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: False)
        path = tmp_path / "mismatched-session.jsonl"
        self._write_minimal_transcript(path, recall_server="dcm-code")

        result = nightly_learn.analyze_mcp_effectiveness(
            [path], project="kubernaut", report_date=nightly_learn.date(2026, 7, 15),
        )

        assert result["exploration_efficiency"]["with_cocoindex"]["sessions"] == 0

    def test_engram_cocoindex_code_bank_is_counted(self, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: False)
        path = tmp_path / "engram-session.jsonl"
        self._write_minimal_transcript(path, recall_server="cocoindex-code")

        result = nightly_learn.analyze_mcp_effectiveness(
            [path], project="engram", report_date=nightly_learn.date(2026, 7, 15),
        )

        assert result["exploration_efficiency"]["with_cocoindex"]["sessions"] == 1

    def test_unknown_project_falls_back_to_kubernaut_recall_banks_without_crashing(self, nightly_learn, tmp_path, monkeypatch):
        monkeypatch.setattr(nightly_learn, "is_correction", lambda text: False)
        path = tmp_path / "unknown-project-session.jsonl"
        self._write_minimal_transcript(path, recall_server="cocoindex-code")

        result = nightly_learn.analyze_mcp_effectiveness(
            [path], project="some-future-project", report_date=nightly_learn.date(2026, 7, 15),
        )

        assert result["exploration_efficiency"]["with_cocoindex"]["sessions"] == 1


class TestProjectConfigsEngram:
    """The engram PROJECT_CONFIGS entry added during the 2026-07-15 Engram
    onboarding -- pins its shape so a future edit can't silently drop a
    required key (e.g. workspace_prefixes, which project-scopes ingestion)."""

    def test_engram_config_has_required_keys(self, nightly_learn):
        cfg = nightly_learn.PROJECT_CONFIGS["engram"]
        assert cfg["log_suffix"] == "-engram"
        assert cfg["workspace_prefixes"] == ["Users-jgil-go-src-github-com-jordigilh-engram"]
        assert "cocoindex-code" in cfg["recall_banks"]
        assert cfg["code_bank"] == "cocoindex-code"
        assert "engram-docs" in cfg["banks"]
        # No issues bank: engram tracks bugs/decisions in docs/FINDINGS.md.
        assert "issues_repos" not in cfg

    def test_engram_mental_models_target_engram_docs_bank(self, nightly_learn):
        cfg = nightly_learn.PROJECT_CONFIGS["engram"]
        assert cfg["mental_models"]["engram-docs"] == ("engram-architecture", "engram-operations")

    def test_kubernaut_and_dcm_have_issues_repos_for_coverage_totals(self, nightly_learn):
        """Regression guard for the collect_ingestion_coverage() fix: DCM's
        GitHub issues/PRs total was always zero because the loop only ever
        queried jordigilh/kubernaut. Both projects must declare their repos."""
        assert "jordigilh/kubernaut" in nightly_learn.PROJECT_CONFIGS["kubernaut"]["issues_repos"]
        assert len(nightly_learn.PROJECT_CONFIGS["kubernaut"]["issues_repos"]) == 4
        assert "dcm-project/dcm" in nightly_learn.PROJECT_CONFIGS["dcm"]["issues_repos"]
        assert len(nightly_learn.PROJECT_CONFIGS["dcm"]["issues_repos"]) == 12


class TestNotifyPendingContradictionsBacklog:
    """notify_pending_contradictions_backlog() is the standing-cadence nudge
    (lever #5 of the 2026-07-14 "reduce input tokens" review) that fires a
    macOS notification when the contradiction-review queue grows past a
    threshold, so clearing the backlog doesn't depend on someone remembering
    to check the dashboard.
    """

    def _patch_state(self, nightly_learn, monkeypatch, tmp_path, threshold=10):
        monkeypatch.setattr(nightly_learn, "CONTRADICTION_NOTIFY_THRESHOLD", threshold)
        monkeypatch.setattr(nightly_learn, "CONTRADICTION_NOTIFY_STATE", tmp_path / "last-notify.txt")

    def test_no_pending_log_skips_without_calling_osascript(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path)
        calls = []
        monkeypatch.setattr(nightly_learn.subprocess, "run", lambda *a, **k: calls.append(a))

        result = nightly_learn.notify_pending_contradictions_backlog(tmp_path / "missing.jsonl")

        assert result == {"pending_count": 0, "notified": False, "skipped_reason": "no_pending_log"}
        assert calls == []

    def test_below_threshold_skips_without_notifying(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=10)
        pending_log = tmp_path / "contradictions-pending.jsonl"
        pending_log.write_text("\n".join('{"id": %d}' % i for i in range(5)) + "\n")
        calls = []
        monkeypatch.setattr(nightly_learn.subprocess, "run", lambda *a, **k: calls.append(a))

        result = nightly_learn.notify_pending_contradictions_backlog(pending_log)

        assert result["pending_count"] == 5
        assert result["notified"] is False
        assert result["skipped_reason"] == "below_threshold"
        assert calls == []

    def test_at_threshold_notifies_and_writes_state(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=10)
        pending_log = tmp_path / "contradictions-pending.jsonl"
        pending_log.write_text("\n".join('{"id": %d}' % i for i in range(12)) + "\n")
        calls = []
        monkeypatch.setattr(nightly_learn.subprocess, "run", lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0})())

        result = nightly_learn.notify_pending_contradictions_backlog(pending_log)

        assert result == {"pending_count": 12, "notified": True, "skipped_reason": None}
        assert len(calls) == 1
        assert calls[0][0][0] == "osascript"
        state_file = nightly_learn.CONTRADICTION_NOTIFY_STATE
        assert state_file.read_text().strip() == nightly_learn.date.today().isoformat()

    def test_regression_already_notified_today_does_not_double_notify(self, nightly_learn, tmp_path, monkeypatch):
        """Regression guard: nightly-learn.py runs once per project (separate
        kubernaut/dcm launchd plists), so a naive "count >= threshold ->
        notify" check would fire twice for the same global backlog every
        night. The per-day state file must make this idempotent."""
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=10)
        pending_log = tmp_path / "contradictions-pending.jsonl"
        pending_log.write_text("\n".join('{"id": %d}' % i for i in range(12)) + "\n")
        nightly_learn.CONTRADICTION_NOTIFY_STATE.write_text(nightly_learn.date.today().isoformat())
        calls = []
        monkeypatch.setattr(nightly_learn.subprocess, "run", lambda *a, **k: calls.append(a))

        result = nightly_learn.notify_pending_contradictions_backlog(pending_log)

        assert result["notified"] is False
        assert result["skipped_reason"] == "already_notified_today"
        assert calls == []

    def test_osascript_failure_does_not_raise(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=10)
        pending_log = tmp_path / "contradictions-pending.jsonl"
        pending_log.write_text("\n".join('{"id": %d}' % i for i in range(12)) + "\n")

        def _raise(*a, **k):
            raise OSError("osascript not found")
        monkeypatch.setattr(nightly_learn.subprocess, "run", _raise)

        result = nightly_learn.notify_pending_contradictions_backlog(pending_log)

        assert result["notified"] is False
        assert "error" in result["skipped_reason"]


class TestMaybeRefreshMentalModelsOnTopicShift:
    """maybe_refresh_mental_models_on_topic_shift() is lever #2 of the
    2026-07-14 "reduce input tokens" review: refresh mental models when
    enough new material has landed since the last refresh, instead of only
    ever refreshing on the nightly cycle.
    """

    def _patch_state(self, nightly_learn, monkeypatch, tmp_path, threshold=5, min_interval_hours=4.0):
        monkeypatch.setattr(nightly_learn, "MODEL_REFRESH_STATE_PATH", tmp_path / "model-refresh-state.json")
        monkeypatch.setattr(nightly_learn, "TOPIC_SHIFT_REFRESH_THRESHOLD", threshold)
        monkeypatch.setattr(nightly_learn, "TOPIC_SHIFT_REFRESH_MIN_INTERVAL_HOURS", min_interval_hours)

    def test_untracked_bank_is_skipped_without_calling_api_post(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path)
        calls = []
        monkeypatch.setattr(nightly_learn, "api_post", lambda *a, **k: calls.append(a) or {"success": True})

        result = nightly_learn.maybe_refresh_mental_models_on_topic_shift("kubernaut-docs", 10)

        assert result["triggered"] is False
        assert result["reason"] == "no_new_items_or_untracked_bank"
        assert calls == []

    def test_zero_new_items_is_skipped(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path)
        calls = []
        monkeypatch.setattr(nightly_learn, "api_post", lambda *a, **k: calls.append(a) or {"success": True})

        result = nightly_learn.maybe_refresh_mental_models_on_topic_shift("cursor-memory", 0)

        assert result["triggered"] is False
        assert calls == []

    def test_below_threshold_accumulates_without_triggering(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=5)
        calls = []
        monkeypatch.setattr(nightly_learn, "api_post", lambda *a, **k: calls.append(a) or {"success": True})

        result = nightly_learn.maybe_refresh_mental_models_on_topic_shift("cursor-memory", 3)

        assert result["triggered"] is False
        assert result["reason"] == "below_threshold"
        assert result["count_since_refresh"] == 3
        assert calls == []

    def test_at_threshold_triggers_refresh_for_every_model_in_the_bank(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=5)
        calls = []
        monkeypatch.setattr(nightly_learn, "api_post", lambda path, payload: calls.append(path) or {"success": True})

        result = nightly_learn.maybe_refresh_mental_models_on_topic_shift("cursor-memory", 6)

        assert result["triggered"] is True
        assert len(calls) == len(nightly_learn.TOPIC_SHIFT_MODELS["cursor-memory"])
        for model_id in nightly_learn.TOPIC_SHIFT_MODELS["cursor-memory"]:
            assert any(model_id in c for c in calls)

    def test_counter_resets_to_zero_after_triggering(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=5)
        monkeypatch.setattr(nightly_learn, "api_post", lambda *a, **k: {"success": True})

        nightly_learn.maybe_refresh_mental_models_on_topic_shift("cursor-memory", 6)
        state = nightly_learn.load_model_refresh_state()

        assert state["cursor-memory"]["count_since_refresh"] == 0
        assert state["cursor-memory"]["last_triggered_at"] is not None

    def test_regression_debounced_within_min_interval_even_above_threshold(self, nightly_learn, tmp_path, monkeypatch):
        """Regression guard: a burst of corrections landing within the
        debounce window must not trigger a second expensive Sonnet
        resynthesis call just because the counter crossed the threshold
        again -- cost containment is the whole point of gating this at
        all, not just the count check."""
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=5, min_interval_hours=4.0)
        calls = []
        monkeypatch.setattr(nightly_learn, "api_post", lambda *a, **k: calls.append(a) or {"success": True})

        first = nightly_learn.maybe_refresh_mental_models_on_topic_shift("cursor-memory", 6)
        assert first["triggered"] is True
        calls.clear()

        second = nightly_learn.maybe_refresh_mental_models_on_topic_shift("cursor-memory", 6)

        assert second["triggered"] is False
        assert second["reason"] == "debounced"
        assert calls == []

    def test_triggers_again_once_min_interval_has_elapsed(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=5, min_interval_hours=4.0)
        monkeypatch.setattr(nightly_learn, "api_post", lambda *a, **k: {"success": True})
        state = {
            "cursor-memory": {
                "count_since_refresh": 0,
                "last_triggered_at": (nightly_learn.datetime.now() - nightly_learn.timedelta(hours=5)).isoformat(),
            },
        }
        nightly_learn.save_model_refresh_state(state)

        result = nightly_learn.maybe_refresh_mental_models_on_topic_shift("cursor-memory", 6)

        assert result["triggered"] is True

    def test_api_post_failure_does_not_raise(self, nightly_learn, tmp_path, monkeypatch):
        self._patch_state(nightly_learn, monkeypatch, tmp_path, threshold=5)

        def _raise(*a, **k):
            raise OSError("connection refused")
        monkeypatch.setattr(nightly_learn, "api_post", _raise)

        result = nightly_learn.maybe_refresh_mental_models_on_topic_shift("cursor-memory", 6)

        assert result["triggered"] is False
        assert "error" in result["reason"]
