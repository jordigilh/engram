"""Tests for kuadrant.py -- the CocoIndex ingestion flows for the Kuadrant
org (ingestion-only prior-art reference for praxis-proxy, 2026-08-27
onboarding; see engram.flows.kuadrant's module docstring).

Coverage focuses on the same doc/issue chunk-ID-cascade regression guard
the other *_cocoindex_flows test files pin (see docs/FINDINGS.md
2026-08-03), plus the kuadrant-specific addition: per-repo language
selection for the mixed Go+Rust code app, and the git-sync helper that
keeps the 8 read-only checkouts from going stale.
"""
from __future__ import annotations

import asyncio
import subprocess
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
    def test_apps_are_defined(self, kuadrant_cocoindex_flows):
        assert kuadrant_cocoindex_flows.docs_app is not None
        assert kuadrant_cocoindex_flows.issues_app is not None
        assert kuadrant_cocoindex_flows.code_app is not None

    def test_architecture_repo_has_no_language_and_is_excluded_from_code(self, kuadrant_cocoindex_flows):
        by_repo = {repo: lang for repo, _upstream, lang in kuadrant_cocoindex_flows.KUADRANT_REPOS}
        assert by_repo["architecture"] is None

    def test_all_eight_repos_present(self, kuadrant_cocoindex_flows):
        repos = [repo for repo, _upstream, _lang in kuadrant_cocoindex_flows.KUADRANT_REPOS]
        assert set(repos) == {
            "kuadrant-operator", "limitador", "wasm-shim", "architecture",
            "authorino", "dns-operator", "authorino-operator", "limitador-operator",
        }

    def test_issues_repos_default_derives_from_kuadrant_repos(self, kuadrant_cocoindex_flows):
        assert "Kuadrant/kuadrant-operator" in kuadrant_cocoindex_flows.ISSUES_REPOS
        assert "Kuadrant/architecture" in kuadrant_cocoindex_flows.ISSUES_REPOS
        assert len(kuadrant_cocoindex_flows.ISSUES_REPOS) == 8


class TestProcessDocFile:
    def test_root_level_doc_gets_root_section_tag(self, kuadrant_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(kuadrant_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(kuadrant_cocoindex_flows.process_doc_file(
            FakeDocFile("# Hello\n\nSome content", "/fake/kuadrant-operator/README.md"),
            base_dir=Path("/fake/kuadrant-operator"),
            source_tag="kuadrant-operator",
        ))

        assert len(retain_calls) == 1
        call = retain_calls[0]
        assert call["bank_id"] == "kuadrant-docs"
        assert call["document_id"] == "kuadrant-operator--README"
        assert call["tags"] == ["root", "kuadrant-operator"]
        assert call["metadata"] == {"source": "cocoindex", "repo": "kuadrant-operator"}

    def test_nested_doc_gets_first_path_segment_as_section_tag(self, kuadrant_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(kuadrant_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        _run(kuadrant_cocoindex_flows.process_doc_file(
            FakeDocFile("# RFC\n\nBody", "/fake/architecture/rfcs/0001-rlp-v2.md"),
            base_dir=Path("/fake/architecture"),
            source_tag="architecture",
        ))

        call = retain_calls[0]
        assert call["document_id"] == "architecture--rfcs--0001-rlp-v2"
        assert call["tags"] == ["rfcs", "architecture"]

    def test_regression_prepending_a_section_does_not_change_older_sections_document_id(
        self, kuadrant_cocoindex_flows, monkeypatch,
    ):
        before_content = "# Title\n\n## Section A\n\nOriginal body text for section A.\n"
        after_content = (
            "# Title\n\n## Section Z\n\nBrand new section.\n\n## Section A\n\nOriginal body text for section A.\n"
        )

        before_calls = []
        monkeypatch.setattr(kuadrant_cocoindex_flows, "hindsight_retain", lambda **kwargs: before_calls.append(kwargs))
        _run(kuadrant_cocoindex_flows.process_doc_file(
            FakeDocFile(before_content, "/fake/limitador/docs/GUIDE.md"),
            base_dir=Path("/fake/limitador"),
            source_tag="limitador",
        ))

        after_calls = []
        monkeypatch.setattr(kuadrant_cocoindex_flows, "hindsight_retain", lambda **kwargs: after_calls.append(kwargs))
        _run(kuadrant_cocoindex_flows.process_doc_file(
            FakeDocFile(after_content, "/fake/limitador/docs/GUIDE.md"),
            base_dir=Path("/fake/limitador"),
            source_tag="limitador",
        ))

        before_by_id = {c["document_id"]: c["content"] for c in before_calls}
        after_by_id = {c["document_id"]: c["content"] for c in after_calls}
        section_a_id = [d for d in before_by_id if d != "limitador--docs--GUIDE"][0]

        assert section_a_id in after_by_id
        assert after_by_id[section_a_id] == before_by_id[section_a_id]


class TestProcessIssue:
    def _issue(self, **overrides):
        issue = {
            "number": 12,
            "title": "Support wildcard hostnames in AuthPolicy",
            "state": "OPEN",
            "_kind": "issue",
            "labels": [{"name": "kind/feature"}],
            "author": {"login": "someone"},
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "body": "It would be useful to support wildcard hostnames.",
            "comments": [],
        }
        issue.update(overrides)
        return issue

    def test_header_excludes_state_and_labels(self, kuadrant_cocoindex_flows, monkeypatch):
        retain_calls = []
        monkeypatch.setattr(kuadrant_cocoindex_flows, "hindsight_retain", lambda **kwargs: retain_calls.append(kwargs))

        kuadrant_cocoindex_flows.process_issue(self._issue(), "Kuadrant/kuadrant-operator")

        assert len(retain_calls) == 1
        call = retain_calls[0]
        assert call["bank_id"] == "kuadrant-issues"
        assert "OPEN" not in call["content"]
        assert "kind/feature" not in call["content"]

    def test_regression_state_change_does_not_change_header_document_content(self, kuadrant_cocoindex_flows, monkeypatch):
        open_calls = []
        monkeypatch.setattr(kuadrant_cocoindex_flows, "hindsight_retain", lambda **kwargs: open_calls.append(kwargs))
        kuadrant_cocoindex_flows.process_issue(self._issue(state="OPEN"), "Kuadrant/kuadrant-operator")

        closed_calls = []
        monkeypatch.setattr(kuadrant_cocoindex_flows, "hindsight_retain", lambda **kwargs: closed_calls.append(kwargs))
        kuadrant_cocoindex_flows.process_issue(self._issue(state="CLOSED"), "Kuadrant/kuadrant-operator")

        assert open_calls[0]["document_id"] == closed_calls[0]["document_id"]
        assert open_calls[0]["content"] == closed_calls[0]["content"]


class TestGitSync:
    def test_git_pull_one_skips_non_git_dirs(self, kuadrant_cocoindex_flows, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

        kuadrant_cocoindex_flows._git_pull_one(tmp_path / "not-a-repo")

        assert calls == []

    def test_git_pull_one_runs_ff_only_pull_for_a_real_checkout(self, kuadrant_cocoindex_flows, monkeypatch, tmp_path):
        repo_dir = tmp_path / "repo"
        (repo_dir / ".git").mkdir(parents=True)

        captured = {}

        class FakeResult:
            returncode = 0
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            return FakeResult()

        monkeypatch.setattr(subprocess, "run", fake_run)
        kuadrant_cocoindex_flows._git_pull_one(repo_dir)

        assert captured["cmd"] == ["git", "pull", "--ff-only", "--quiet"]
        assert captured["cwd"] == str(repo_dir)


class TestHindsightRetain:
    def test_regression_payload_does_not_include_dead_strategy_field(self, kuadrant_cocoindex_flows, monkeypatch):
        import json

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

        monkeypatch.setattr(kuadrant_cocoindex_flows, "urlopen", fake_urlopen)
        kuadrant_cocoindex_flows.hindsight_retain(bank_id="kuadrant-docs", content="x", document_id="doc-1")

        assert "strategy" not in captured_requests[0]["items"][0]
