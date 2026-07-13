"""Tests for project_scope.py -- the shared onboarded-project allowlist gate
for the cursor-memory retain pipeline -- and for
purge-out-of-scope-memories.py's classification logic that relies on it.
"""
from __future__ import annotations

import json

import project_scope as ps


class TestIsAllowedWorkspace:
    def test_exact_kubernaut_match(self):
        assert ps.is_allowed_workspace("Users-jgil-go-src-github-com-jordigilh-kubernaut") is True

    def test_kubernaut_sibling_repo_matches_via_prefix(self):
        assert ps.is_allowed_workspace("Users-jgil-go-src-github-com-jordigilh-kubernaut-operator") is True
        assert ps.is_allowed_workspace("Users-jgil-go-src-github-com-jordigilh-kubernaut-docs") is True

    def test_dcm_project_matches(self):
        assert ps.is_allowed_workspace("Users-jgil-go-src-github-com-dcm-project-enhancements") is True

    def test_engram_matches(self):
        assert ps.is_allowed_workspace("Users-jgil-go-src-github-com-jordigilh-engram") is True

    def test_out_of_scope_workspace_rejected(self):
        assert ps.is_allowed_workspace("Users-jgil-go-src-github-com-insights-onprem-koku") is False
        assert ps.is_allowed_workspace("Users-jgil-go-src-github-com-redhat-developer-rhdh-plugins") is False

    def test_empty_window_session_rejected(self):
        assert ps.is_allowed_workspace("empty-window") is False

    def test_bare_numeric_workspace_rejected(self):
        assert ps.is_allowed_workspace("1776029340207") is False

    def test_substring_match_is_not_enough_must_be_prefix(self):
        """A workspace name that merely *contains* an allowed prefix (but
        doesn't start with it) must not match -- guards against a naive
        `in` check being swapped in for `startswith`."""
        assert ps.is_allowed_workspace("foo-Users-jgil-go-src-github-com-jordigilh-kubernaut") is False

    def test_case_sensitive(self):
        assert ps.is_allowed_workspace("users-jgil-go-src-github-com-jordigilh-kubernaut") is False

    def test_empty_string_rejected(self):
        assert ps.is_allowed_workspace("") is False


class TestTranscriptGlobPatterns:
    def test_one_pattern_per_allowed_prefix(self):
        patterns = ps.transcript_glob_patterns()
        assert len(patterns) == len(ps.ALLOWED_WORKSPACE_PREFIXES)

    def test_each_pattern_is_prefix_star_plus_transcripts_suffix(self):
        for prefix, pattern in zip(ps.ALLOWED_WORKSPACE_PREFIXES, ps.transcript_glob_patterns()):
            assert pattern == f"{prefix}*/agent-transcripts/**/*.jsonl"


class TestPurgeScriptClassification:
    def test_build_transcript_project_map_walks_agent_transcripts_dirs(self, purge_script, tmp_path, monkeypatch):
        allowed_proj = tmp_path / "Users-jgil-go-src-github-com-jordigilh-engram"
        (allowed_proj / "agent-transcripts").mkdir(parents=True)
        (allowed_proj / "agent-transcripts" / "transcript-abc.jsonl").write_text("{}")

        disallowed_proj = tmp_path / "Users-jgil-go-src-github-com-insights-onprem-koku"
        (disallowed_proj / "agent-transcripts").mkdir(parents=True)
        (disallowed_proj / "agent-transcripts" / "transcript-xyz.jsonl").write_text("{}")

        no_transcripts_dir = tmp_path / "some-other-dir"
        no_transcripts_dir.mkdir()

        monkeypatch.setattr(purge_script, "PROJECTS_ROOT", tmp_path)
        mapping = purge_script.build_transcript_project_map()

        assert mapping["transcript-abc"] == "Users-jgil-go-src-github-com-jordigilh-engram"
        assert mapping["transcript-xyz"] == "Users-jgil-go-src-github-com-insights-onprem-koku"
        assert "some-other-dir" not in mapping.values()

    def test_classify_buckets_documents_correctly(self, purge_script):
        tid_to_project = {
            "transcript-in-scope": "Users-jgil-go-src-github-com-jordigilh-engram",
            "transcript-out-of-scope": "Users-jgil-go-src-github-com-insights-onprem-koku",
        }
        docs = [
            {"id": "doc-1", "document_metadata": {"transcript_id": "transcript-in-scope"}},
            {"id": "doc-2", "document_metadata": {"transcript_id": "transcript-out-of-scope"}},
            {"id": "doc-3", "document_metadata": {}},  # no transcript_id
            {"id": "doc-4", "document_metadata": {"transcript_id": "transcript-unknown"}},  # unresolved
            {"id": "doc-5"},  # missing document_metadata entirely
        ]

        buckets = purge_script.classify(docs, tid_to_project)

        assert [d["id"] for d in buckets["to_delete"]] == ["doc-2"]
        assert buckets["to_delete"][0]["_resolved_project"] == "Users-jgil-go-src-github-com-insights-onprem-koku"
        assert {d["id"] for d in buckets["no_transcript_id"]} == {"doc-3", "doc-5"}
        assert [d["id"] for d in buckets["unresolved"]] == ["doc-4"]

    def test_classify_leaves_in_scope_documents_in_no_bucket(self, purge_script):
        tid_to_project = {"t1": "Users-jgil-go-src-github-com-jordigilh-kubernaut"}
        docs = [{"id": "doc-1", "document_metadata": {"transcript_id": "t1"}}]

        buckets = purge_script.classify(docs, tid_to_project)

        assert buckets["to_delete"] == []
        assert buckets["no_transcript_id"] == []
        assert buckets["unresolved"] == []
