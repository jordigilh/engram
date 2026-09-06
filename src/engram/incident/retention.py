"""Durable, compact incident knowledge for the Kubernaut PoC.

Raw evidence is deliberately not stored here. The store keeps promoted incident
dossiers, recurring failure families, provenance references, and optional change
relationships. SQLite is used for the PoC; the schema uses portable SQL types.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    family_signature TEXT NOT NULL,
    rr_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    test_name TEXT NOT NULL,
    classification TEXT NOT NULL,
    symptom TEXT NOT NULL,
    cause TEXT NOT NULL,
    confidence REAL NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    resolution TEXT,
    validated_by TEXT,
    metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failure_families (
    family_signature TEXT PRIMARY KEY,
    occurrence_count INTEGER NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    canonical_cause TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_refs (
    incident_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    reference_json TEXT NOT NULL,
    PRIMARY KEY (incident_id, evidence_id),
    FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
);
CREATE TABLE IF NOT EXISTS changes (
    change_id TEXT PRIMARY KEY,
    commit_sha TEXT,
    change_type TEXT NOT NULL,
    component TEXT,
    changed_at TEXT,
    metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_changes (
    incident_id TEXT NOT NULL,
    change_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (incident_id, change_id),
    FOREIGN KEY (incident_id) REFERENCES incidents(incident_id),
    FOREIGN KEY (change_id) REFERENCES changes(change_id)
);
"""


def _connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def family_signature(context: dict[str, Any]) -> str:
    summary = context.get("summary", {})
    material = {
        "test_name": context.get("test", {}).get("name"),
        "workflow_resolution_failed": summary.get("workflow_resolution_failed"),
        "manual_review_required": summary.get("manual_review_required"),
        "operator_escalation": summary.get("operator_escalation"),
        "project": context.get("scope", {}).get("project", "kubernaut"),
        "branch": context.get("scope", {}).get("branch", "main"),
    }
    return "family-" + hashlib.sha1(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def _time_bounds(context: dict[str, Any]) -> tuple[str | None, str | None]:
    timestamps = [item["timestamp"] for item in context.get("timeline", []) if item.get("timestamp")]
    return (min(timestamps), max(timestamps)) if timestamps else (None, None)


def _classification(context: dict[str, Any]) -> tuple[str, str]:
    summary = context.get("summary", {})
    if summary.get("workflow_resolution_failed"):
        return "workflow_resolution_failure", "WorkflowResolutionFailed/operator_escalation"
    return "unclassified_failure", "Unclassified failure"


def promote_incident(
    context: dict[str, Any],
    db_path: Path,
    *,
    validated_by: str | None = None,
    resolution: str | None = None,
    changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist a compact incident dossier and aggregate its failure family."""
    test = context.get("test", {})
    rr_id = context["rr_id"]
    scope = context.get("scope", {})
    incident_id = f"incident-{scope.get('project', 'kubernaut')}-{scope.get('branch', 'main')}-{rr_id}"
    family = family_signature(context)
    classification, cause = _classification(context)
    first_seen, last_seen = _time_bounds(context)
    metadata = {
        "confidence_basis": context.get("confidence_basis", []),
        "affected_namespaces": context.get("summary", {}).get("affected_namespaces", []),
    }
    with _connection(db_path) as connection:
        existing = connection.execute("SELECT 1 FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        connection.execute(
            """INSERT INTO incidents
            (incident_id, family_signature, rr_id, run_id, job_id, test_name,
             classification, symptom, cause, confidence, first_seen, last_seen,
             resolution, validated_by, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(incident_id) DO UPDATE SET
              confidence = excluded.confidence,
              last_seen = excluded.last_seen,
              resolution = COALESCE(excluded.resolution, incidents.resolution),
              validated_by = COALESCE(excluded.validated_by, incidents.validated_by),
              metadata_json = excluded.metadata_json""",
            (
                incident_id,
                family,
                rr_id,
                test.get("run_id", ""),
                test.get("job_id", ""),
                test.get("name", ""),
                classification,
                "WorkflowExecution was not created for the correlated RR",
                cause,
                float(context.get("confidence", 0.0)),
                first_seen,
                last_seen,
                resolution,
                validated_by,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        if existing is None:
            connection.execute(
                """INSERT INTO failure_families
                (family_signature, occurrence_count, first_seen, last_seen, canonical_cause, metadata_json)
                VALUES (?, 1, ?, ?, ?, ?) ON CONFLICT(family_signature) DO UPDATE SET
                  occurrence_count = failure_families.occurrence_count + 1,
                  last_seen = excluded.last_seen""",
                (family, first_seen, last_seen, cause, json.dumps(metadata, sort_keys=True)),
            )
        for evidence in context.get("evidence", []):
            connection.execute(
                "INSERT OR REPLACE INTO evidence_refs VALUES (?, ?, ?, ?)",
                (incident_id, evidence["id"], evidence.get("source_file", ""), json.dumps(evidence, default=str)),
            )
        for change in changes or []:
            change_id = change.get("change_id") or change.get("commit_sha") or f"change-{hashlib.sha1(json.dumps(change, sort_keys=True).encode()).hexdigest()[:16]}"
            connection.execute(
                "INSERT OR REPLACE INTO changes VALUES (?, ?, ?, ?, ?, ?)",
                (change_id, change.get("commit_sha"), change.get("change_type", "unknown"), change.get("component"), change.get("changed_at"), json.dumps(change, sort_keys=True)),
            )
            connection.execute(
                "INSERT OR REPLACE INTO incident_changes VALUES (?, ?, ?, ?, ?)",
                (incident_id, change_id, change.get("relation", "temporally_related"), float(change.get("confidence", 0.5)), json.dumps(change, sort_keys=True)),
            )
    return {"incident_id": incident_id, "family_signature": family, "new_incident": existing is None}


def failure_history(db_path: Path, signature: str) -> list[dict[str, Any]]:
    with _connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM incidents WHERE family_signature = ? ORDER BY first_seen", (signature,)
        ).fetchall()
    return [dict(row) for row in rows]


def incident_timeline(db_path: Path, incident_id: str) -> dict[str, Any]:
    with _connection(db_path) as connection:
        incident = connection.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        changes = connection.execute(
            """SELECT c.*, ic.relation, ic.confidence AS relation_confidence, ic.evidence_json
               FROM incident_changes ic JOIN changes c ON c.change_id = ic.change_id
               WHERE ic.incident_id = ? ORDER BY c.changed_at""",
            (incident_id,),
        ).fetchall()
    return {"incident": dict(incident) if incident else None, "changes": [dict(row) for row in changes]}
