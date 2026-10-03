from __future__ import annotations

import asyncio
import json
from pathlib import Path

from engram import contradiction_resolution
from engram.flows import configured
from engram.project_config import ProjectIssueSource, ProjectSource, effective_source_tag


class FakeFile:
    def __init__(self, content: str, path: Path):
        self._content = content
        self.file_path = path

    async def read_text(self) -> str:
        return self._content


def run(coro):
    return asyncio.run(coro)


def test_source_tags_and_serialization_preserve_release_scope():
    source = ProjectSource(
        tag="repo",
        root=Path("/checkout"),
        docs_include=(),
        docs_exclude=(),
        code_include=("**/*.go",),
        code_exclude=(),
        language="go",
        branch="v1.5",
    )

    assert effective_source_tag(source) == "repo@release-v1.5"
    payload = json.loads(configured._source_json((source,)))
    assert payload == [{
        "tag": "repo@release-v1.5",
        "root": "/checkout",
        "docs_include": [],
        "docs_exclude": [],
        "code_include": ["**/*.go"],
        "code_exclude": [],
        "language": "go",
        "document_format": "markdown",
        "branch": "v1.5",
    }]


def test_default_flow_apps_keep_transcripts_opt_in():
    assert configured._default_selected_apps({"docs": object(), "transcripts": object()}) == {"docs"}


def test_document_processing_is_source_and_bank_configured(monkeypatch, tmp_path):
    retained = []
    monkeypatch.setattr(configured, "synthesize_document", lambda *_: {"key_sentences": [], "keywords": []})
    monkeypatch.setattr(configured, "hindsight_retain", lambda **kwargs: retained.append(kwargs))

    run(configured.process_doc_file(
        FakeFile("# Title\n\nBody", tmp_path / "docs" / "README.md"),
        tmp_path / "docs",
        "demo-docs",
        "repo",
    ))

    assert len(retained) == 1
    assert retained[0]["bank_id"] == "demo-docs"
    assert retained[0]["document_id"] == "repo--README"
    assert retained[0]["tags"] == ["root", "repo"]
    assert retained[0]["metadata"]["repo"] == "repo"


def test_pdf_processing_uses_same_configured_document_path(monkeypatch, tmp_path):
    import pdfplumber

    retained = []
    monkeypatch.setattr(configured, "synthesize_document", lambda *_: {"key_sentences": [], "keywords": []})
    monkeypatch.setattr(configured, "hindsight_retain", lambda **kwargs: retained.append(kwargs))

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Pdf:
        pages = [Page("first page"), Page(" "), Page("third page")]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(pdfplumber, "open", lambda _path: Pdf())
    path = tmp_path / "manual" / "guide.pdf"
    run(configured.process_pdf_file(FakeFile("", path), tmp_path / "manual", "demo-docs", "manual"))

    assert retained[0]["document_id"] == "manual--guide"
    assert "--- Page 1 ---" in retained[0]["content"]
    assert "--- Page 3 ---" in retained[0]["content"]
    assert "--- Page 2 ---" not in retained[0]["content"]


def test_issue_provider_serialization_uses_connector_field_names():
    source = ProjectIssueSource(
        provider="github_projects",
        github_organization="example",
        github_project_numbers=(2, 7),
    )

    payload = configured._issue_provider_json(source)

    assert payload["github_organization"] == "example"
    assert payload["github_project_numbers"] == [2, 7]
    assert "organization" not in payload
    assert "project_numbers" not in payload


def test_issue_and_jira_connectors_retain_into_the_selected_bank(monkeypatch):
    retained = []
    monkeypatch.setattr(configured, "hindsight_retain", lambda **kwargs: retained.append(kwargs))
    issue = {
        "number": 4,
        "title": "A useful issue",
        "body": "A sufficiently long issue body for the generic connector.",
        "state": "OPEN",
        "_kind": "pr",
        "author": {"login": "alice"},
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "labels": [],
        "comments": [],
    }
    configured.process_issue(issue, "org/repo", "issues", document_prefix="github")

    jira = {
        "key": "EX-4",
        "fields": {
            "summary": "Jira item",
            "description": "Plain text description",
            "status": {"name": "Open"},
            "issuetype": {"name": "Story"},
            "labels": [],
            "reporter": {"displayName": "bob"},
            "updated": "2026-01-02T00:00:00Z",
            "comment": {"comments": []},
        },
    }
    configured.process_jira_issue(jira, "issues", document_prefix="jira", project_name="EX")

    assert retained[0]["document_id"] == "github-repo-pr-4"
    assert retained[0]["metadata"]["repo"] == "org/repo"
    assert retained[1]["document_id"] == "jira-EX-4"
    assert retained[1]["metadata"]["tracker"] == "jira"


def test_transcript_user_query_extraction_and_project_tagging(monkeypatch, tmp_path):
    transcript_root = tmp_path / "projects"
    transcript = transcript_root / "workspace-demo" / "agent-transcripts" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({
        "role": "user",
        "message": {"content": [{"type": "text", "text": "<user_query>Always use tests before proceeding.</user_query>"}]},
    }) + "\n")
    retained = []
    monkeypatch.setattr(configured, "hindsight_retain", lambda **kwargs: retained.append(kwargs))
    monkeypatch.setattr(configured, "_TRANSCRIPT_WATERMARKS_PATH", tmp_path / "watermarks.json")

    run(configured.process_transcript(
        FakeFile(transcript.read_text(), transcript),
        "shared-memory",
        transcript_root,
        json.dumps(["workspace-"]),
        "demo",
    ))

    assert len(retained) == 1
    assert "Always use tests" in retained[0]["content"]
    assert "<user_query>" not in retained[0]["content"]
    assert retained[0]["tags"] == ["demo"]


def test_queued_transcript_correction_is_not_retained(monkeypatch, tmp_path):
    transcript_root = tmp_path / "projects"
    transcript = transcript_root / "workspace-demo" / "agent-transcripts" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({
        "role": "user",
        "message": {"content": "No, that's wrong; use the other approach."},
    }) + "\n")
    retained = []
    monkeypatch.setattr(configured, "hindsight_retain", lambda **kwargs: retained.append(kwargs))
    monkeypatch.setattr(configured, "_TRANSCRIPT_WATERMARKS_PATH", tmp_path / "watermarks.json")
    monkeypatch.setattr(
        contradiction_resolution,
        "resolve",
        lambda *args, **kwargs: contradiction_resolution.Resolution(action="queued"),
    )

    run(configured.process_transcript(
        FakeFile(transcript.read_text(), transcript),
        "shared-memory",
        transcript_root,
        json.dumps(["workspace-"]),
        "demo",
    ))

    assert retained == []
