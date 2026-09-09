"""Tests for engram.synthesis (deterministic, zero-LLM document synthesis)."""

from engram import synthesis


DOC = """# Migration Plan

The team agreed Thursday to postpone the migration until Q2, with Priya
owning the rollback plan. This decision affects three services.

## Steps

- Freeze the staging environment before Friday deploy freeze begins.
- Notify all downstream consumers of the new timeline and rollback plan.
- Priya will draft the rollback runbook by end of week for review.
"""


def test_deterministic_across_runs() -> None:
    first = synthesis.synthesize_document("d", DOC)
    second = synthesis.synthesize_document("d", DOC)
    assert first == second


def test_key_sentences_come_from_source() -> None:
    result = synthesis.synthesize_document("d", DOC)
    assert result["doc_id"] == "d"
    assert 1 <= len(result["key_sentences"]) <= synthesis.MAX_SENTENCES
    for s in result["key_sentences"]:
        assert s in DOC
    assert result["stats"]["coverage_chars"] > 0


def test_keywords_are_content_terms() -> None:
    result = synthesis.synthesize_document("d", DOC)
    assert result["keywords"], "expected topical keywords"
    assert "migration" in result["keywords"] or "rollback" in result["keywords"]


def test_empty_and_code_only_inputs() -> None:
    empty = synthesis.synthesize_document("e", "")
    assert empty["key_sentences"] == [] and empty["keywords"] == []
    fenced = synthesis.synthesize_document("c", "```\nx = 1\n```\n")
    assert fenced["key_sentences"] == []


def test_metadata_is_json_serializable() -> None:
    import json

    result = synthesis.synthesize_document("d", DOC)
    json.dumps({"key_sentences": result["key_sentences"],
                "keywords": result["keywords"]})
