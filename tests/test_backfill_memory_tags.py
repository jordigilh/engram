"""Tests for backfill-memory-tags.py's plan_retags(): the pure
document-to-project resolution logic used to retag the pre-existing,
untagged cursor-memory backlog (2026-07-27 fix, see docs/FINDINGS.md).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


_spec = importlib.util.spec_from_file_location(
    "backfill_memory_tags", Path(__file__).resolve().parent.parent / "backfill-memory-tags.py"
)
bmt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bmt)


def _doc(doc_id, tags=None, transcript_id=None):
    return {
        "id": doc_id,
        "tags": tags or [],
        "document_metadata": {"transcript_id": transcript_id} if transcript_id else {},
    }


class TestPlanRetags:
    def test_resolves_kubernaut_document(self):
        docs = [_doc("d1", transcript_id="t1")]
        tid_to_workspace = {"t1": "Users-jgil-go-src-github-com-jordigilh-kubernaut"}

        plan = bmt.plan_retags(docs, tid_to_workspace)

        assert plan == [{"document_id": "d1", "project": "kubernaut"}]

    def test_resolves_dcm_document(self):
        docs = [_doc("d1", transcript_id="t1")]
        tid_to_workspace = {"t1": "Users-jgil-go-src-github-com-dcm-project-cli"}

        plan = bmt.plan_retags(docs, tid_to_workspace)

        assert plan == [{"document_id": "d1", "project": "dcm"}]

    def test_already_tagged_document_is_skipped(self):
        docs = [_doc("d1", tags=["kubernaut"], transcript_id="t1")]
        tid_to_workspace = {"t1": "Users-jgil-go-src-github-com-jordigilh-kubernaut"}

        plan = bmt.plan_retags(docs, tid_to_workspace)

        assert plan == [], "already-tagged documents must not be replanned (idempotency)"

    def test_document_with_no_transcript_id_is_skipped(self):
        docs = [{"id": "d1", "tags": [], "document_metadata": {"source": "triage-rearrange"}}]

        plan = bmt.plan_retags(docs, tid_to_workspace={})

        assert plan == []

    def test_transcript_file_not_on_disk_is_skipped(self):
        docs = [_doc("d1", transcript_id="t-missing")]

        plan = bmt.plan_retags(docs, tid_to_workspace={})

        assert plan == []

    def test_empty_window_workspace_is_skipped(self):
        docs = [_doc("d1", transcript_id="t1")]
        tid_to_workspace = {"t1": "empty-window"}

        plan = bmt.plan_retags(docs, tid_to_workspace)

        assert plan == [], "blank/no-folder sessions have no project to attribute"

    def test_out_of_scope_workspace_is_skipped(self):
        docs = [_doc("d1", transcript_id="t1")]
        tid_to_workspace = {"t1": "Users-jgil-go-src-github-com-someorg-unrelated-repo"}

        plan = bmt.plan_retags(docs, tid_to_workspace)

        assert plan == []

    def test_mixed_batch_only_plans_resolvable_documents(self):
        docs = [
            _doc("resolvable", transcript_id="t1"),
            _doc("already-tagged", tags=["dcm"], transcript_id="t2"),
            _doc("no-tid"),
            _doc("missing-file", transcript_id="t-gone"),
        ]
        tid_to_workspace = {
            "t1": "Users-jgil-go-src-github-com-jordigilh-kubernaut",
            "t2": "Users-jgil-go-src-github-com-dcm-project-cli",
        }

        plan = bmt.plan_retags(docs, tid_to_workspace)

        assert plan == [{"document_id": "resolvable", "project": "kubernaut"}]
