"""Tests for praxis-cocoindex-flows.py -- the CocoIndex ingestion flows added
during the praxis-proxy org onboarding into Hindsight+CocoIndex.

Coverage focuses on:
- the same doc/issue chunk-ID-cascade regression guard the other
  *_cocoindex_flows test files pin (see docs/FINDINGS.md 2026-08-03)
- the praxis-specific additions over the dcm/koku reference pattern: issue
  milestone metadata, GitHub Discussions ingestion (with the deliberately
  relaxed comment filter -- no TRUSTED_ASSOCIATIONS gate, unlike issues), and
  org Project (v2) board Status snapshot formatting.
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


class FakePdfPage:
    """Minimal stand-in for a pdfplumber page, matching process_pdf_file's
    usage (page.extract_text())."""

    def __init__(self, text: str):
        self._text = text

    def extract_text(self) -> str:
        return self._text


class FakePdf:
    """Minimal stand-in for pdfplumber's PDF context manager, matching
    process_pdf_file's usage (`with pdfplumber.open(path) as pdf: pdf.pages`)."""

    def __init__(self, page_texts: list[str]):
        self.pages = [FakePdfPage(t) for t in page_texts]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(coro):
    return asyncio.run(coro)


class TestModuleLoads:
    def test_apps_are_defined(self, praxis_cocoindex_flows):
        assert praxis_cocoindex_flows.docs_app is not None
        assert praxis_cocoindex_flows.issues_app is not None
        assert praxis_cocoindex_flows.discussions_app is not None
        assert praxis_cocoindex_flows.roadmap_app is not None
        assert praxis_cocoindex_flows.code_app is not None

    def test_praxis_repos_excludes_pingora(self, praxis_cocoindex_flows):
        upstreams = [upstream for _, upstream, _ in praxis_cocoindex_flows.PRAXIS_REPOS]
        assert "praxis-proxy/pingora" not in upstreams
        assert "praxis-proxy/grid" in upstreams
        assert "praxis-proxy/ai" in upstreams


class TestProcessDocFile:
    def test_root_level_doc_gets_root_section_tag(self, praxis_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(praxis_cocoindex_flows.process_doc_file(
            FakeDocFile("# Hello\n\nSome content", "/fake/praxis-grid/docs/foo.md"),
            base_dir=Path("/fake/praxis-grid"),
            source_tag="praxis-grid",
        ))

        assert len(retain_calls) == 1
        call = retain_calls[0]
        assert call["bank_id"] == "praxis-docs"
        assert call["document_id"] == "praxis-grid--docs--foo"
        assert call["tags"] == ["docs", "praxis-grid"]

    def test_regression_prepending_a_section_does_not_change_older_sections_document_id(
        self, praxis_cocoindex_flows, monkeypatch,
    ):
        before_content = "# Title\n\n## Section A\n\nOriginal body text for section A.\n"
        after_content = (
            "# Title\n\n## Section Z\n\nBrand new section.\n\n## Section A\n\nOriginal body text for section A.\n"
        )

        before_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: before_calls.append(kwargs))
        _run(praxis_cocoindex_flows.process_doc_file(
            FakeDocFile(before_content, "/fake/praxis-grid/docs/GUIDE.md"),
            base_dir=Path("/fake/praxis-grid"),
            source_tag="praxis-grid",
        ))

        after_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: after_calls.append(kwargs))
        _run(praxis_cocoindex_flows.process_doc_file(
            FakeDocFile(after_content, "/fake/praxis-grid/docs/GUIDE.md"),
            base_dir=Path("/fake/praxis-grid"),
            source_tag="praxis-grid",
        ))

        before_by_id = {c["document_id"]: c["content"] for c in before_calls}
        after_by_id = {c["document_id"]: c["content"] for c in after_calls}
        section_a_id = [d for d in before_by_id if d != "praxis-grid--docs--GUIDE"][0]

        assert section_a_id in after_by_id
        assert after_by_id[section_a_id] == before_by_id[section_a_id]


class TestProcessPdfFile:
    def test_pdf_pages_are_joined_with_page_markers_and_retained_into_praxis_docs(
        self, praxis_cocoindex_flows, monkeypatch,
    ):
        import pdfplumber

        retain_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))
        monkeypatch.setattr(pdfplumber, "open", lambda path: FakePdf(["Page one text.", "Page two text."]))

        _run(praxis_cocoindex_flows.process_pdf_file(
            FakeDocFile("", "/fake/manual-docs/ai-gateway-project.pdf"),
            base_dir=Path("/fake/manual-docs"),
            source_tag="praxis-manual-docs",
        ))

        assert len(retain_calls) == 1
        call = retain_calls[0]
        assert call["bank_id"] == "praxis-docs"
        assert call["document_id"] == "praxis-manual-docs--ai-gateway-project"
        assert call["tags"] == ["root", "praxis-manual-docs"]
        assert "--- Page 1 ---" in call["content"] and "Page one text." in call["content"]
        assert "--- Page 2 ---" in call["content"] and "Page two text." in call["content"]

    def test_blank_pages_are_skipped_but_others_still_join(self, praxis_cocoindex_flows, monkeypatch):
        import pdfplumber

        retain_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))
        monkeypatch.setattr(pdfplumber, "open", lambda path: FakePdf(["Front matter.", "   ", "Real content."]))

        _run(praxis_cocoindex_flows.process_pdf_file(
            FakeDocFile("", "/fake/manual-docs/ai-grid-project.pdf"),
            base_dir=Path("/fake/manual-docs"),
            source_tag="praxis-manual-docs",
        ))

        content = retain_calls[0]["content"]
        assert "Front matter." in content
        assert "Real content." in content
        # The blank middle page contributes no "--- Page 2 ---" marker of its
        # own -- only non-blank pages are joined in, so the 2nd marker present
        # is for the 3rd (real-content) page.
        assert "--- Page 3 ---" in content
        assert "--- Page 2 ---" not in content

    def test_all_blank_pages_retains_nothing(self, praxis_cocoindex_flows, monkeypatch):
        import pdfplumber

        retain_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))
        monkeypatch.setattr(pdfplumber, "open", lambda path: FakePdf(["", "   ", ""]))

        _run(praxis_cocoindex_flows.process_pdf_file(
            FakeDocFile("", "/fake/manual-docs/blank-scan.pdf"),
            base_dir=Path("/fake/manual-docs"),
            source_tag="praxis-manual-docs",
        ))

        assert retain_calls == []

    def test_document_ids_are_stable_across_reruns(self, praxis_cocoindex_flows, monkeypatch):
        import pdfplumber

        monkeypatch.setattr(pdfplumber, "open", lambda path: FakePdf(["Page one text.", "Page two text."]))

        first_calls: list = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: first_calls.append(kwargs))
        _run(praxis_cocoindex_flows.process_pdf_file(
            FakeDocFile("", "/fake/manual-docs/ai-gateway-project.pdf"),
            base_dir=Path("/fake/manual-docs"),
            source_tag="praxis-manual-docs",
        ))

        second_calls: list = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: second_calls.append(kwargs))
        _run(praxis_cocoindex_flows.process_pdf_file(
            FakeDocFile("", "/fake/manual-docs/ai-gateway-project.pdf"),
            base_dir=Path("/fake/manual-docs"),
            source_tag="praxis-manual-docs",
        ))

        assert [c["document_id"] for c in first_calls] == [c["document_id"] for c in second_calls]


class TestProcessIssue:
    def _issue(self, **overrides):
        issue = {
            "number": 74,
            "title": "Epic: Mixture-of-Models / Intelligent Routing",
            "state": "OPEN",
            "_kind": "issue",
            "labels": [{"name": "needs-triage"}],
            "milestone": {"title": "v0.2.0", "dueOn": "2026-08-06T00:00:00Z"},
            "author": {"login": "usize"},
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "body": "This epic tracks all work needed to handle intelligent routing.",
            "comments": [],
        }
        issue.update(overrides)
        return issue

    def _comment(self, author="bob", body="a real substantive review comment", association="MEMBER"):
        return {"author": {"login": author}, "body": body, "authorAssociation": association}

    def test_header_excludes_state_labels_and_milestone(self, praxis_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        praxis_cocoindex_flows.process_issue(self._issue(), "praxis-proxy/ai")

        assert len(retain_calls) == 1
        assert "OPEN" not in retain_calls[0]["content"]
        assert "needs-triage" not in retain_calls[0]["content"]
        assert "v0.2.0" not in retain_calls[0]["content"]

    def test_milestone_carried_in_metadata_and_tags(self, praxis_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        praxis_cocoindex_flows.process_issue(self._issue(), "praxis-proxy/ai")

        call = retain_calls[0]
        assert call["metadata"]["milestone"] == "v0.2.0"
        assert call["metadata"]["milestone_due"] == "2026-08-06T00:00:00Z"
        assert "milestone:v0.2.0" in call["tags"]

    def test_no_milestone_omits_milestone_metadata(self, praxis_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        praxis_cocoindex_flows.process_issue(self._issue(milestone=None), "praxis-proxy/ai")

        call = retain_calls[0]
        assert "milestone" not in call["metadata"]
        assert not any(t.startswith("milestone:") for t in call["tags"])

    def test_regression_state_change_does_not_change_header_document_content(self, praxis_cocoindex_flows, monkeypatch):
        open_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: open_calls.append(kwargs))
        praxis_cocoindex_flows.process_issue(self._issue(state="OPEN"), "praxis-proxy/ai")

        closed_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: closed_calls.append(kwargs))
        praxis_cocoindex_flows.process_issue(self._issue(state="CLOSED"), "praxis-proxy/ai")

        assert open_calls[0]["document_id"] == closed_calls[0]["document_id"]
        assert open_calls[0]["content"] == closed_calls[0]["content"]

    def test_regression_new_comment_does_not_change_earlier_comment_document_ids(self, praxis_cocoindex_flows, monkeypatch):
        first_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: first_calls.append(kwargs))
        praxis_cocoindex_flows.process_issue(
            self._issue(comments=[self._comment(body="the first review comment")]), "praxis-proxy/ai",
        )

        second_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: second_calls.append(kwargs))
        praxis_cocoindex_flows.process_issue(
            self._issue(comments=[
                self._comment(body="the first review comment"),
                self._comment(body="the second review comment"),
            ]),
            "praxis-proxy/ai",
        )

        first_by_id = {c["document_id"]: c["content"] for c in first_calls}
        second_by_id = {c["document_id"]: c["content"] for c in second_calls}
        for doc_id, content in first_by_id.items():
            assert second_by_id[doc_id] == content
        assert "ai-issue-74-comment1" in second_by_id
        assert "ai-issue-74-comment1" not in first_by_id


class TestProcessDiscussion:
    def _discussion(self, **overrides):
        disc = {
            "number": 838,
            "title": "Model rewrite machinery",
            "url": "https://github.com/orgs/praxis-proxy/discussions/838",
            "category": {"name": "Ideas"},
            "author": {"login": "usize"},
            "createdAt": "2026-07-24T00:00:00Z",
            "updatedAt": "2026-07-24T01:15:14Z",
            "body": "We should consider abstracting the machinery of rewriting the model field.",
            "comments": {"nodes": []},
        }
        disc.update(overrides)
        return disc

    def test_discussion_document_and_metadata_shape(self, praxis_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(praxis_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        praxis_cocoindex_flows.process_discussion(self._discussion(), "praxis-proxy/praxis")

        assert len(retain_calls) == 1
        call = retain_calls[0]
        assert call["bank_id"] == "praxis-issues"
        assert call["document_id"] == "praxis-discussion-838"
        assert call["metadata"]["kind"] == "discussion"
        assert call["metadata"]["category"] == "Ideas"
        assert "discussion" in call["tags"]

    def test_comment_filter_keeps_none_association_unlike_issue_comments(self, praxis_cocoindex_flows, monkeypatch):
        """Real-world motivation: ai#74's most substantive comment (an
        external vllm-project contributor proposing Conversational Routing
        Momentum) had authorAssociation=NONE. Issue comments filter that
        out via TRUSTED_ASSOCIATIONS; discussion comments must not, or this
        org's open-source technical input gets silently dropped."""
        disc = self._discussion(comments={"nodes": [
            {"author": {"login": "external-contributor"}, "body": "A genuinely substantive external comment here.", "authorAssociation": "NONE"},
        ]})
        filtered = praxis_cocoindex_flows._filter_discussion_comments(disc)
        assert len(filtered) == 1
        assert filtered[0]["author"]["login"] == "external-contributor"

    def test_comment_filter_still_excludes_bots(self, praxis_cocoindex_flows):
        disc = self._discussion(comments={"nodes": [
            {"author": {"login": "dependabot[bot]"}, "body": "Bumping some dependency version here.", "authorAssociation": "NONE"},
        ]})
        filtered = praxis_cocoindex_flows._filter_discussion_comments(disc)
        assert filtered == []


class TestBoardSnapshot:
    def _item(self, number, repo, title, status):
        return {
            "content": {"number": number, "title": title, "repository": {"name": repo}},
            "fieldValues": {"nodes": [
                {"name": status, "field": {"name": "Status"}},
            ]},
        }

    def test_item_status_extracts_status_field(self, praxis_cocoindex_flows):
        item = self._item(193, "ai", "Epic: AI Grid Capabilities", "Epics")
        assert praxis_cocoindex_flows._item_status(item) == "Epics"

    def test_item_status_defaults_when_no_status_field(self, praxis_cocoindex_flows):
        item = {"content": {}, "fieldValues": {"nodes": []}}
        assert praxis_cocoindex_flows._item_status(item) == "(no status)"

    def test_snapshot_groups_by_status_in_priority_order(self, praxis_cocoindex_flows):
        items = [
            self._item(201, "ai", "Remote site backend integration", "Next"),
            self._item(193, "ai", "Epic: AI Grid Capabilities", "Epics"),
            self._item(172, "ai", "Spike: OpenShell + Praxis", "In Progress"),
        ]
        content = praxis_cocoindex_flows._format_board_snapshot(3, "AI Grid", "Grid networks for AI inference", items)

        # In Progress must be listed before Next, and Next before Epics --
        # this is the actual "what's staffed right now" signal this model
        # exists to answer.
        assert content.index("Status: In Progress") < content.index("Status: Next")
        assert content.index("Status: Next") < content.index("Status: Epics")
        assert "#193" in content and "#201" in content and "#172" in content


class TestHindsightRetain:
    def test_regression_payload_does_not_include_dead_strategy_field(self, praxis_cocoindex_flows, monkeypatch):
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

        monkeypatch.setattr(praxis_cocoindex_flows, "urlopen", fake_urlopen)
        praxis_cocoindex_flows.hindsight_retain(bank_id="praxis-docs", content="x", document_id="doc-1")

        assert "strategy" not in captured_requests[0]["items"][0]
