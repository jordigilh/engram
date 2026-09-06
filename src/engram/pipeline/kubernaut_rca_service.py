"""Kubernaut-only MCP service for deterministic RCA evidence retrieval."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from engram.incident.service import triage_test_failure as build_triage_context
from engram.incident.remote import ingest_urls
from engram.incident.retention import (
    failure_history,
    family_signature,
    incident_timeline,
    promote_incident as store_incident,
)
from engram.incident.postgres_retention import (
    failure_history_pg,
    incident_timeline_pg,
    promote_incident_pg,
)


def _run_mcp_server(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8897,
    transport: str = "stdio",
    db_path: Path | None = None,
    pg_url: str | None = None,
) -> None:
    from mcp.server.mcpserver import MCPServer as FastMCP

    import json

    mcp = FastMCP("kubernaut-rca")
    latest_context: dict | None = None
    active_root = root
    retention_path = db_path or Path(os.environ.get("KUBERNAUT_RCA_DB", "~/.hindsight/kubernaut-rca.sqlite3")).expanduser()
    latest_changes: list[dict] = []

    @mcp.tool()
    def ingest_test_run(
        test_log_url: str,
        must_gather_url: str,
        commit_sha: str | None = None,
        repository: str | None = None,
        workflow: str | None = None,
    ) -> str:
        """Download and index one GitHub Actions job log and must-gather artifact.

        Only GitHub Actions job and artifact URLs are accepted. Authentication is
        read from GH_TOKEN or GITHUB_TOKEN in the service environment.
        """
        nonlocal active_root, latest_changes
        manifest = ingest_urls(test_log_url, must_gather_url)
        manifest["commit_sha"] = commit_sha
        manifest["repository"] = repository
        manifest["workflow"] = workflow
        latest_changes = []
        if commit_sha:
            latest_changes.append(
                {
                    "commit_sha": commit_sha,
                    "change_type": "source_commit",
                    "component": repository,
                    "relation": "incident_commit",
                    "confidence": 0.5,
                }
            )
        active_root = Path(manifest["root"])
        return json.dumps(manifest)

    @mcp.tool()
    def promote_incident(
        validated_by: str | None = None,
        resolution: str | None = None,
        changes: list[dict] | None = None,
    ) -> str:
        """Persist the latest compact dossier without retaining raw logs."""
        if not latest_context:
            return json.dumps({"error": "triage_test_failure must be called first"})
        if pg_url:
            result = promote_incident_pg(
                latest_context, pg_url, validated_by=validated_by,
                resolution=resolution, changes=changes or latest_changes,
            )
        else:
            result = store_incident(
                latest_context, retention_path, validated_by=validated_by,
                resolution=resolution, changes=changes or latest_changes,
            )
        return json.dumps(result)

    @mcp.tool()
    def get_failure_history(failure_family: str | None = None) -> str:
        """Return promoted incidents in one recurring failure family."""
        signature = failure_family
        if not signature and latest_context:
            signature = family_signature(latest_context)
        if not signature:
            return json.dumps({"error": "provide failure_family or triage an incident first"})
        incidents = failure_history_pg(pg_url, signature) if pg_url else failure_history(retention_path, signature)
        return json.dumps({"family_signature": signature, "incidents": incidents}, default=str)

    @mcp.tool()
    def get_incident_timeline(incident_id: str) -> str:
        """Return a promoted incident and its linked changes."""
        timeline = incident_timeline_pg(pg_url, incident_id) if pg_url else incident_timeline(retention_path, incident_id)
        return json.dumps(timeline, default=str)

    @mcp.tool()
    def triage_test_failure(
        run_id: str,
        job_id: str,
        test_name: str,
        failure_text: str,
        rr_id: str | None = None,
        max_tokens: int = 12000,
    ) -> str:
        """Return a bounded RCA dossier correlated by Kubernaut RR ID."""
        nonlocal latest_context
        latest_context = build_triage_context(
            root=active_root,
            run_id=run_id,
            job_id=job_id,
            test_name=test_name,
            failure_text=failure_text,
            rr_id=rr_id,
            max_tokens=min(max_tokens, 20000),
        )
        return json.dumps(latest_context, default=str)

    @mcp.tool()
    def get_evidence(evidence_id: str) -> str:
        """Return one evidence item from the most recent triage dossier."""
        if not latest_context:
            return json.dumps({"error": "triage_test_failure must be called first"})
        for item in latest_context["evidence"]:
            if item["id"] == evidence_id:
                return json.dumps(item, default=str)
        return json.dumps({"error": f"unknown evidence id: {evidence_id}"})

    @mcp.tool()
    def get_related_events(evidence_id: str) -> str:
        """Return timeline records related to one evidence item."""
        if not latest_context:
            return json.dumps({"error": "triage_test_failure must be called first"})
        related = [item for item in latest_context["timeline"] if item["evidence_id"] == evidence_id]
        return json.dumps({"evidence_id": evidence_id, "events": related}, default=str)

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=transport, host=host, port=port)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("KUBERNAUT_MUST_GATHER", ".")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8897)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--pg-url", default=os.environ.get("KUBERNAUT_RCA_PG_URL"))
    args = parser.parse_args()
    _run_mcp_server(args.root, args.host, args.port, args.transport, args.db, args.pg_url)


if __name__ == "__main__":
    main()
