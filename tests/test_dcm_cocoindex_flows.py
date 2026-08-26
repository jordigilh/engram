"""Tests for dcm-cocoindex-flows.py -- the CocoIndex ingestion flows added
during DCM's onboarding into Hindsight+CocoIndex.

Mirrors test_cocoindex_flows.py's/test_koku_cocoindex_flows.py's coverage of
the 2026-08-03 chunk-ID-cascade fix (chunking.py) for docs and issues -- see
docs/FINDINGS.md. This file's PG_POOL ContextKey collision with
cocoindex-flows.py's own was fixed as part of this same change specifically
so dcm-cocoindex-flows.py could get direct test coverage for the first time.
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
    def test_apps_are_defined(self, dcm_cocoindex_flows):
        assert dcm_cocoindex_flows.docs_app is not None
        assert dcm_cocoindex_flows.issues_app is not None
        assert dcm_cocoindex_flows.code_app is not None

    def test_osac_project_osac_is_registered(self, dcm_cocoindex_flows):
        """osac-project/osac (upstream OSAC backend, read-only) is folded
        into dcm rather than getting its own PROJECT_CONFIGS entry -- see
        DCM_OSAC_DIR's comment. Must show up in issues polling and default
        to the watch-mirror worktree, not a live dev clone."""
        assert dcm_cocoindex_flows.DCM_OSAC_DIR is not None
        assert str(dcm_cocoindex_flows.DCM_OSAC_DIR).endswith(".hindsight/watch/osac")
        assert "osac-project/osac" in dcm_cocoindex_flows.ISSUES_REPOS


class TestProcessDocFile:
    def test_root_level_doc_gets_root_section_tag(self, dcm_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(dcm_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(dcm_cocoindex_flows.process_doc_file(
            FakeDocFile("# Hello\n\nSome content", "/fake/repo/docs/foo.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="dcm",
        ))

        assert len(retain_calls) == 1
        call = retain_calls[0]
        assert call["bank_id"] == "dcm-docs"
        assert call["document_id"] == "dcm--foo"
        assert call["tags"] == ["root", "dcm"]

    def test_regression_prepending_a_section_does_not_change_older_sections_document_id(
        self, dcm_cocoindex_flows, monkeypatch,
    ):
        before_content = "# Title\n\n## Section A\n\nOriginal body text for section A.\n"
        after_content = (
            "# Title\n\n## Section Z\n\nBrand new section.\n\n## Section A\n\nOriginal body text for section A.\n"
        )

        before_calls = []
        monkeypatch.setattr(dcm_cocoindex_flows, "hindsight_retain", lambda **kwargs: before_calls.append(kwargs))
        _run(dcm_cocoindex_flows.process_doc_file(
            FakeDocFile(before_content, "/fake/repo/docs/GUIDE.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="dcm",
        ))

        after_calls = []
        monkeypatch.setattr(dcm_cocoindex_flows, "hindsight_retain", lambda **kwargs: after_calls.append(kwargs))
        _run(dcm_cocoindex_flows.process_doc_file(
            FakeDocFile(after_content, "/fake/repo/docs/GUIDE.md"),
            base_dir=Path("/fake/repo/docs"),
            source_tag="dcm",
        ))

        before_by_id = {c["document_id"]: c["content"] for c in before_calls}
        after_by_id = {c["document_id"]: c["content"] for c in after_calls}
        section_a_id = [d for d in before_by_id if d != "dcm--GUIDE"][0]

        assert section_a_id in after_by_id
        assert after_by_id[section_a_id] == before_by_id[section_a_id]


class TestProcessIssue:
    def _issue(self, **overrides):
        issue = {
            "number": 9,
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

    def _comment(self, author="bob", body="a real substantive discussion comment", association="MEMBER"):
        return {"author": {"login": author}, "body": body, "authorAssociation": association}

    def test_header_only_issue_produces_single_bare_document_id(self, dcm_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(dcm_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        dcm_cocoindex_flows.process_issue(self._issue(), "dcm-project/dcm")

        assert len(retain_calls) == 1
        assert retain_calls[0]["document_id"] == "dcm-issue-9"
        assert "OPEN" not in retain_calls[0]["content"]
        assert "bug" not in retain_calls[0]["content"]

    def test_regression_state_change_does_not_change_header_document_content(self, dcm_cocoindex_flows, monkeypatch):
        open_calls = []
        monkeypatch.setattr(dcm_cocoindex_flows, "hindsight_retain", lambda **kwargs: open_calls.append(kwargs))
        dcm_cocoindex_flows.process_issue(self._issue(state="OPEN"), "dcm-project/dcm")

        closed_calls = []
        monkeypatch.setattr(dcm_cocoindex_flows, "hindsight_retain", lambda **kwargs: closed_calls.append(kwargs))
        dcm_cocoindex_flows.process_issue(self._issue(state="CLOSED"), "dcm-project/dcm")

        assert open_calls[0]["document_id"] == closed_calls[0]["document_id"]
        assert open_calls[0]["content"] == closed_calls[0]["content"]

    def test_regression_new_comment_does_not_change_earlier_comment_document_ids(self, dcm_cocoindex_flows, monkeypatch):
        first_calls = []
        monkeypatch.setattr(dcm_cocoindex_flows, "hindsight_retain", lambda **kwargs: first_calls.append(kwargs))
        dcm_cocoindex_flows.process_issue(
            self._issue(comments=[self._comment(body="the first discussion comment")]), "dcm-project/dcm",
        )

        second_calls = []
        monkeypatch.setattr(dcm_cocoindex_flows, "hindsight_retain", lambda **kwargs: second_calls.append(kwargs))
        dcm_cocoindex_flows.process_issue(
            self._issue(comments=[
                self._comment(body="the first discussion comment"),
                self._comment(body="the second discussion comment"),
            ]),
            "dcm-project/dcm",
        )

        first_by_id = {c["document_id"]: c["content"] for c in first_calls}
        second_by_id = {c["document_id"]: c["content"] for c in second_calls}
        for doc_id, content in first_by_id.items():
            assert second_by_id[doc_id] == content
        assert "dcm-issue-9-comment1" in second_by_id
        assert "dcm-issue-9-comment1" not in first_by_id


class TestHindsightRetain:
    def test_regression_payload_does_not_include_dead_strategy_field(self, dcm_cocoindex_flows, monkeypatch):
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

        monkeypatch.setattr(dcm_cocoindex_flows, "urlopen", fake_urlopen)
        dcm_cocoindex_flows.hindsight_retain(bank_id="dcm-docs", content="x", document_id="doc-1")

        assert "strategy" not in captured_requests[0]["items"][0]
