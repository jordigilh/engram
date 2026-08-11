"""Tests for koku-cocoindex-flows.py -- the CocoIndex ingestion flows added
during Koku's onboarding into Hindsight+CocoIndex.

Coverage focuses on the same things test_engram_cocoindex_flows.py and
test_cocoindex_flows.py pin for their own doc/issue processing: path ->
document_id derivation for docs, and the 2026-08-03 chunk-ID-cascade fix
(chunking.py) for both docs and issues/Jira tickets -- see docs/FINDINGS.md.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path


class FakeDocFile:
    """Minimal stand-in for CocoIndex's localfs.File, matching
    process_doc_file's usage (file.read_text(), file.file_path.resolve())."""

    def __init__(self, content: str, path: str):
        self._content = content
        self.file_path = Path(path)

    async def read_text(self) -> str:
        return self._content


def _run(coro):
    return asyncio.run(coro)


class TestModuleLoads:
    def test_apps_are_defined(self, koku_cocoindex_flows):
        assert koku_cocoindex_flows.docs_app is not None
        assert koku_cocoindex_flows.issues_app is not None
        assert koku_cocoindex_flows.code_app is not None

    def test_koku_service_operator_folded_into_koku_scope(self, koku_cocoindex_flows):
        """koku-service-operator (2026-08-10) is folded into koku's existing
        banks/table rather than getting its own PROJECT_CONFIGS entry -- see
        the module docstring. PR_REPOS must include it (it has zero GitHub
        Issues of its own, verified against `gh issue list`, so it only ever
        shows up via PR_REPOS, never a separate issues-only list)."""
        assert "project-koku/koku-service-operator" in koku_cocoindex_flows.PR_REPOS
        assert "project-koku/koku" in koku_cocoindex_flows.PR_REPOS
        assert koku_cocoindex_flows.KOKU_SERVICE_OPERATOR_REPO_DIR.name == "koku-service-operator"

    def test_pr_limit_defaults_to_2000(self, koku_cocoindex_flows):
        """koku is 5+ years old with a large PR history; only the most recent
        PRs carry task-relevant signal for temporal (non-core) contributors
        -- see docs/FINDINGS.md 2026-08-11."""
        assert koku_cocoindex_flows.KOKU_PR_LIMIT == 2000


class TestFetchAllPrs:
    def test_gh_pr_list_invoked_with_configured_limit(self, koku_cocoindex_flows, monkeypatch):
        """gh pr list defaults to newest-created-first, so --limit is a
        recency window, not an arbitrary truncation -- verified live 2026-08-11."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

        monkeypatch.setattr(koku_cocoindex_flows.subprocess, "run", fake_run)
        koku_cocoindex_flows._fetch_all_prs("project-koku/koku")

        assert "--limit" in captured_cmd
        limit_value = captured_cmd[captured_cmd.index("--limit") + 1]
        assert limit_value == str(koku_cocoindex_flows.KOKU_PR_LIMIT)


class TestProcessDocFile:
    def test_root_level_doc_gets_root_section_tag(self, koku_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(koku_cocoindex_flows.process_doc_file(
            FakeDocFile("# Hello\n\nSome content", "/fake/repo/docs/foo.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="koku-docs-tree",
        ))

        assert len(retain_calls) == 1
        call = retain_calls[0]
        assert call["bank_id"] == "koku-docs"
        assert call["document_id"] == "koku-docs-tree--foo"
        assert call["tags"] == ["root", "koku-docs-tree"]

    def test_regression_prepending_a_section_does_not_change_older_sections_document_id(
        self, koku_cocoindex_flows, monkeypatch,
    ):
        before_content = "# Title\n\n## Section A\n\nOriginal body text for section A.\n"
        after_content = (
            "# Title\n\n## Section Z\n\nBrand new section.\n\n## Section A\n\nOriginal body text for section A.\n"
        )

        before_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: before_calls.append(kwargs))
        _run(koku_cocoindex_flows.process_doc_file(
            FakeDocFile(before_content, "/fake/repo/docs/GUIDE.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="koku-docs-tree",
        ))

        after_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: after_calls.append(kwargs))
        _run(koku_cocoindex_flows.process_doc_file(
            FakeDocFile(after_content, "/fake/repo/docs/GUIDE.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="koku-docs-tree",
        ))

        before_by_id = {c["document_id"]: c["content"] for c in before_calls}
        after_by_id = {c["document_id"]: c["content"] for c in after_calls}
        section_a_id = [d for d in before_by_id if d != "koku-docs-tree--GUIDE"][0]

        assert section_a_id in after_by_id
        assert after_by_id[section_a_id] == before_by_id[section_a_id]


class TestProcessPr:
    def _pr(self, **overrides):
        pr = {
            "number": 7,
            "title": "Fix the thing",
            "state": "OPEN",
            "_kind": "pr",
            "labels": [{"name": "bug"}],
            "author": {"login": "alice"},
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "body": "This PR fixes the thing that was broken.",
            "comments": [],
        }
        pr.update(overrides)
        return pr

    def _comment(self, author="bob", body="a real substantive review comment", association="MEMBER"):
        return {"author": {"login": author}, "body": body, "authorAssociation": association}

    def test_header_excludes_state_and_labels(self, koku_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        koku_cocoindex_flows.process_pr(self._pr(), "project-koku/koku")

        assert len(retain_calls) == 1
        assert "OPEN" not in retain_calls[0]["content"]
        assert "bug" not in retain_calls[0]["content"]

    def test_regression_state_change_does_not_change_header_document_content(self, koku_cocoindex_flows, monkeypatch):
        open_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: open_calls.append(kwargs))
        koku_cocoindex_flows.process_pr(self._pr(state="OPEN"), "project-koku/koku")

        merged_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: merged_calls.append(kwargs))
        koku_cocoindex_flows.process_pr(self._pr(state="MERGED"), "project-koku/koku")

        assert open_calls[0]["document_id"] == merged_calls[0]["document_id"]
        assert open_calls[0]["content"] == merged_calls[0]["content"]

    def test_regression_new_comment_does_not_change_earlier_comment_document_ids(self, koku_cocoindex_flows, monkeypatch):
        first_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: first_calls.append(kwargs))
        koku_cocoindex_flows.process_pr(self._pr(comments=[self._comment(body="the first review comment")]), "project-koku/koku")

        second_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: second_calls.append(kwargs))
        koku_cocoindex_flows.process_pr(
            self._pr(comments=[
                self._comment(body="the first review comment"),
                self._comment(body="the second review comment"),
            ]),
            "project-koku/koku",
        )

        first_by_id = {c["document_id"]: c["content"] for c in first_calls}
        second_by_id = {c["document_id"]: c["content"] for c in second_calls}
        for doc_id, content in first_by_id.items():
            assert second_by_id[doc_id] == content
        assert "koku-pr-7-comment1" in second_by_id
        assert "koku-pr-7-comment1" not in first_by_id


class TestProcessJiraIssue:
    def _issue(self, status="New", priority="Normal", labels=None, comments=None):
        return {
            "key": "COST-123",
            "fields": {
                "summary": "Something is broken",
                "status": {"name": status},
                "issueType": {"name": "Bug"},
                "priority": {"name": priority},
                "labels": labels or [],
                "reporter": {"displayName": "alice"},
                "created": "2026-01-01T00:00:00.000+0000",
                "updated": "2026-01-02T00:00:00.000+0000",
                "description": {"type": "doc", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Full description here."}]},
                ]},
                "comment": {"comments": comments or []},
            },
        }

    def _comment(self, author="bob", text="a real substantive comment body"):
        return {
            "author": {"displayName": author},
            "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]},
        }

    def test_header_excludes_status_priority_and_labels(self, koku_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        koku_cocoindex_flows.process_jira_issue(self._issue(labels=["urgent"]))

        assert len(retain_calls) == 1
        assert "New" not in retain_calls[0]["content"]
        assert "Normal" not in retain_calls[0]["content"]
        assert "urgent" not in retain_calls[0]["content"]

    def test_regression_status_change_does_not_change_header_document_content(self, koku_cocoindex_flows, monkeypatch):
        new_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: new_calls.append(kwargs))
        koku_cocoindex_flows.process_jira_issue(self._issue(status="New"))

        done_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: done_calls.append(kwargs))
        koku_cocoindex_flows.process_jira_issue(self._issue(status="Done"))

        assert new_calls[0]["document_id"] == done_calls[0]["document_id"]
        assert new_calls[0]["content"] == done_calls[0]["content"]
        assert new_calls[0]["metadata"]["status"] == "new"
        assert done_calls[0]["metadata"]["status"] == "done"

    def test_regression_new_comment_does_not_change_earlier_comment_document_ids(self, koku_cocoindex_flows, monkeypatch):
        first_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: first_calls.append(kwargs))
        koku_cocoindex_flows.process_jira_issue(self._issue(comments=[self._comment(text="the first comment body text")]))

        second_calls = []
        monkeypatch.setattr(koku_cocoindex_flows, "hindsight_retain", lambda **kwargs: second_calls.append(kwargs))
        koku_cocoindex_flows.process_jira_issue(self._issue(comments=[
            self._comment(text="the first comment body text"),
            self._comment(text="the second comment body text"),
        ]))

        first_by_id = {c["document_id"]: c["content"] for c in first_calls}
        second_by_id = {c["document_id"]: c["content"] for c in second_calls}
        for doc_id, content in first_by_id.items():
            assert second_by_id[doc_id] == content
        assert "koku-jira-COST-123-comment1" in second_by_id
        assert "koku-jira-COST-123-comment1" not in first_by_id


class TestFetchAllJiraIssues:
    """KOKU_JIRA_LIMIT (2026-08-11): same recency-cap rationale as
    KOKU_PR_LIMIT, applied to Jira -- see docs/FINDINGS.md."""

    def _fake_page(self, n: int, is_last: bool, next_token: str | None = None) -> dict:
        return {
            "issues": [{"key": f"COST-{i}"} for i in range(n)],
            "isLast": is_last,
            "nextPageToken": next_token,
        }

    def test_default_limit_is_2000(self, koku_cocoindex_flows):
        assert koku_cocoindex_flows.KOKU_JIRA_LIMIT == 2000

    def test_jql_sorts_by_created_desc_for_recency(self, koku_cocoindex_flows, monkeypatch):
        captured_bodies = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self._payload).encode()

        def fake_urlopen(req, timeout=60):
            captured_bodies.append(json.loads(req.data))
            return FakeResponse(self._fake_page(5, is_last=True))

        monkeypatch.setattr(koku_cocoindex_flows, "_jira_token", lambda: "fake-token")
        monkeypatch.setattr(koku_cocoindex_flows, "urlopen", fake_urlopen)

        koku_cocoindex_flows._fetch_all_jira_issues("COST", limit=10)

        assert "order by created desc" in captured_bodies[0]["jql"]

    def test_stops_paginating_once_limit_reached(self, koku_cocoindex_flows, monkeypatch):
        pages = [
            self._fake_page(100, is_last=False, next_token="tok-1"),
            self._fake_page(100, is_last=False, next_token="tok-2"),
        ]
        call_count = {"n": 0}

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self._payload).encode()

        def fake_urlopen(req, timeout=60):
            page = pages[call_count["n"]]
            call_count["n"] += 1
            return FakeResponse(page)

        monkeypatch.setattr(koku_cocoindex_flows, "_jira_token", lambda: "fake-token")
        monkeypatch.setattr(koku_cocoindex_flows, "urlopen", fake_urlopen)

        result = koku_cocoindex_flows._fetch_all_jira_issues("COST", page_size=100, limit=150)

        assert len(result) == 150
        assert call_count["n"] == 2


class TestHindsightRetain:
    def test_regression_payload_does_not_include_dead_strategy_field(self, koku_cocoindex_flows, monkeypatch):
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

        monkeypatch.setattr(koku_cocoindex_flows, "urlopen", fake_urlopen)
        koku_cocoindex_flows.hindsight_retain(bank_id="koku-docs", content="x", document_id="doc-1")

        assert "strategy" not in captured_requests[0]["items"][0]
