import json

from engram.pipeline.kubernaut_rca_service import lookup_indexed_evidence


def test_lookup_indexed_evidence_resolves_records_outside_bounded_dossier(tmp_path) -> None:
    index = tmp_path / ".engram" / "evidence-index.jsonl"
    index.parent.mkdir()
    index.write_text(
        json.dumps(
            {
                "id": "evidence-outside-dossier",
                "evidence_type": "log_event",
                "source_file": "logs/controller.log",
                "content": "full evidence",
                "metadata": {"structured": {"kind": "WorkflowExecution"}},
            }
        )
        + "\n"
    )

    result = lookup_indexed_evidence(tmp_path, "evidence-outside-dossier")

    assert result == {
        "id": "evidence-outside-dossier",
        "type": "log_event",
        "timestamp": None,
        "source_file": "logs/controller.log",
        "source_line": None,
        "identifiers": {},
        "metadata": {"structured": {"kind": "WorkflowExecution"}},
        "content": "full evidence",
    }
