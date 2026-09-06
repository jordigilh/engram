"""PostgreSQL retention backend for the Kubernaut incident PoC."""
from __future__ import annotations

import json
from typing import Any

from .retention import SCHEMA, _classification, _time_bounds, family_signature


def _connect(pg_url: str):
    import psycopg2
    from psycopg2.extras import RealDictCursor

    connection = psycopg2.connect(pg_url)
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA IF NOT EXISTS cocoindex")
        cursor.execute("SET search_path TO cocoindex, public")
        cursor.execute(SCHEMA)
    connection.commit()
    return connection, RealDictCursor


def promote_incident_pg(
    context: dict[str, Any],
    pg_url: str,
    *,
    validated_by: str | None = None,
    resolution: str | None = None,
    changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    test = context.get("test", {})
    rr_id = context["rr_id"]
    scope = context.get("scope", {})
    incident_id = f"incident-{scope.get('project', 'kubernaut')}-{scope.get('branch', 'main')}-{rr_id}"
    family = family_signature(context)
    classification, cause = _classification(context)
    first_seen, last_seen = _time_bounds(context)
    metadata = json.dumps({
        "confidence_basis": context.get("confidence_basis", []),
        "affected_namespaces": context.get("summary", {}).get("affected_namespaces", []),
    }, sort_keys=True)
    connection, cursor_factory = _connect(pg_url)
    try:
        with connection.cursor(cursor_factory=cursor_factory) as cursor:
            cursor.execute("SELECT 1 FROM incidents WHERE incident_id = %s", (incident_id,))
            is_new = cursor.fetchone() is None
            cursor.execute(
                """INSERT INTO incidents
                (incident_id, family_signature, rr_id, run_id, job_id, test_name,
                 classification, symptom, cause, confidence, first_seen, last_seen,
                 resolution, validated_by, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (incident_id) DO UPDATE SET
                  confidence = EXCLUDED.confidence,
                  last_seen = EXCLUDED.last_seen,
                  resolution = COALESCE(EXCLUDED.resolution, incidents.resolution),
                  validated_by = COALESCE(EXCLUDED.validated_by, incidents.validated_by),
                  metadata_json = EXCLUDED.metadata_json""",
                (incident_id, family, rr_id, test.get("run_id", ""), test.get("job_id", ""),
                 test.get("name", ""), classification,
                 "WorkflowExecution was not created for the correlated RR", cause,
                 float(context.get("confidence", 0.0)), first_seen, last_seen,
                 resolution, validated_by, metadata),
            )
            cursor.execute(
                """INSERT INTO failure_families
                (family_signature, occurrence_count, first_seen, last_seen, canonical_cause, metadata_json)
                VALUES (%s, 1, %s, %s, %s, %s)
                ON CONFLICT (family_signature) DO UPDATE SET
                  occurrence_count = failure_families.occurrence_count + 1,
                  last_seen = EXCLUDED.last_seen""",
                (family, first_seen, last_seen, cause, metadata),
            ) if is_new else None
            for evidence in context.get("evidence", []):
                cursor.execute(
                    """INSERT INTO evidence_refs (incident_id, evidence_id, source_file, reference_json)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (incident_id, evidence_id) DO UPDATE SET reference_json = EXCLUDED.reference_json""",
                    (incident_id, evidence["id"], evidence.get("source_file", ""), json.dumps(evidence, default=str)),
                )
            for change in changes or []:
                change_id = change.get("change_id") or change.get("commit_sha") or "change-" + str(abs(hash(json.dumps(change, sort_keys=True))))
                cursor.execute(
                    """INSERT INTO changes (change_id, commit_sha, change_type, component, changed_at, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (change_id) DO UPDATE SET metadata_json = EXCLUDED.metadata_json""",
                    (change_id, change.get("commit_sha"), change.get("change_type", "unknown"), change.get("component"), change.get("changed_at"), json.dumps(change, sort_keys=True)),
                )
                cursor.execute(
                    """INSERT INTO incident_changes (incident_id, change_id, relation, confidence, evidence_json)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (incident_id, change_id) DO UPDATE SET confidence = EXCLUDED.confidence""",
                    (incident_id, change_id, change.get("relation", "temporally_related"), float(change.get("confidence", 0.5)), json.dumps(change, sort_keys=True)),
                )
        connection.commit()
    finally:
        connection.close()
    return {"incident_id": incident_id, "family_signature": family, "new_incident": is_new}


def failure_history_pg(pg_url: str, signature: str) -> list[dict[str, Any]]:
    connection, cursor_factory = _connect(pg_url)
    try:
        with connection.cursor(cursor_factory=cursor_factory) as cursor:
            cursor.execute("SELECT * FROM incidents WHERE family_signature = %s ORDER BY first_seen", (signature,))
            return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def incident_timeline_pg(pg_url: str, incident_id: str) -> dict[str, Any]:
    connection, cursor_factory = _connect(pg_url)
    try:
        with connection.cursor(cursor_factory=cursor_factory) as cursor:
            cursor.execute("SELECT * FROM incidents WHERE incident_id = %s", (incident_id,))
            incident = cursor.fetchone()
            cursor.execute(
                """SELECT c.*, ic.relation, ic.confidence AS relation_confidence, ic.evidence_json
                FROM incident_changes ic JOIN changes c ON c.change_id = ic.change_id
                WHERE ic.incident_id = %s ORDER BY c.changed_at""",
                (incident_id,),
            )
            changes = cursor.fetchall()
        return {"incident": dict(incident) if incident else None, "changes": [dict(row) for row in changes]}
    finally:
        connection.close()
