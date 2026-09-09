"""Kubernaut-only MCP service for deterministic RCA evidence retrieval."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from engram.incident.service import triage_test_failure as build_triage_context
from engram.incident.remote import ingest_urls
from engram.incident.branch_scope import normalize_branch
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
    contexts: dict[tuple[str, str], dict] = {}
    roots: dict[tuple[str, str], Path] = {}
    dossiers_by_scope: dict[tuple[str, str], list[dict]] = {}
    retention_path = db_path or Path(os.environ.get("KUBERNAUT_RCA_DB", "~/.engram/kubernaut-rca.sqlite3")).expanduser()
    changes_by_scope: dict[tuple[str, str], list[dict]] = {}

    def scope_key(project: str, branch: str) -> tuple[str, str]:
        if project not in {"kubernaut", "kubernaut-operator"}:
            raise ValueError("RCA service is restricted to the Kubernaut project family")
        return project, normalize_branch(branch)

    @mcp.tool()
    def ingest_test_run(
        test_log_url: str,
        must_gather_url: str,
        commit_sha: str | None = None,
        repository: str | None = None,
        workflow: str | None = None,
        branch: str = "main",
        project: str = "kubernaut",
    ) -> str:
        """Download and index one GitHub Actions job log and must-gather artifact.

        Only GitHub Actions job and artifact URLs are accepted. Authentication is
        read from GH_TOKEN or GITHUB_TOKEN in the service environment.
        """
        key = scope_key(project, branch)
        manifest = ingest_urls(test_log_url, must_gather_url)
        manifest["commit_sha"] = commit_sha
        manifest["repository"] = repository
        manifest["workflow"] = workflow
        changes_by_scope[key] = []
        if commit_sha:
            changes_by_scope[key].append(
                {
                    "commit_sha": commit_sha,
                    "change_type": "source_commit",
                    "component": repository,
                    "relation": "incident_commit",
                    "confidence": 0.5,
                }
            )
        roots[key] = Path(manifest["root"])
        return json.dumps(manifest)

    @mcp.tool()
    def promote_incident(
        branch: str = "main",
        project: str = "kubernaut",
        validated_by: str | None = None,
        resolution: str | None = None,
        changes: list[dict] | None = None,
    ) -> str:
        """Persist the latest compact dossier without retaining raw logs."""
        context = contexts.get(scope_key(project, branch))
        if not context:
            return json.dumps({"error": "triage_test_failure must be called first"})
        if pg_url:
            result = promote_incident_pg(
                context, pg_url, validated_by=validated_by,
                resolution=resolution, changes=changes or changes_by_scope.get(scope_key(project, branch), []),
            )
        else:
            result = store_incident(
                context, retention_path, validated_by=validated_by,
                resolution=resolution, changes=changes or changes_by_scope.get(scope_key(project, branch), []),
            )
        return json.dumps(result)

    @mcp.tool()
    def get_failure_history(
        branch: str = "main", project: str = "kubernaut", failure_family: str | None = None
    ) -> str:
        """Return promoted incidents in one recurring failure family."""
        signature = failure_family
        context = contexts.get(scope_key(project, branch))
        if not signature and context:
            signature = family_signature(context)
        if not signature:
            return json.dumps({"error": "provide failure_family or triage an incident first"})
        incidents = failure_history_pg(pg_url, signature) if pg_url else failure_history(retention_path, signature)
        return json.dumps({"family_signature": signature, "incidents": incidents}, default=str)

    @mcp.tool()
    def get_incident_timeline(incident_id: str, branch: str = "main", project: str = "kubernaut") -> str:
        """Return a promoted incident and its linked changes."""
        normalized_branch = normalize_branch(branch)
        expected_prefix = f"incident-{project}-{normalized_branch}-"
        if not incident_id.startswith(expected_prefix):
            return json.dumps({"error": "incident does not belong to the requested project/branch scope"})
        timeline = incident_timeline_pg(pg_url, incident_id) if pg_url else incident_timeline(retention_path, incident_id)
        return json.dumps(timeline, default=str)

    @mcp.tool()
    def triage_test_failure(
        run_id: str,
        job_id: str,
        test_name: str,
        failure_text: str,
        rr_id: str | None = None,
        branch: str = "main",
        project: str = "kubernaut",
        max_tokens: int = 12000,
    ) -> str:
        """Return a bounded RCA dossier correlated by Kubernaut RR ID."""
        key = scope_key(project, branch)
        context = build_triage_context(
            root=roots.get(key, root),
            run_id=run_id,
            job_id=job_id,
            test_name=test_name,
            failure_text=failure_text,
            rr_id=rr_id,
            branch=branch,
            project=project,
            max_tokens=min(max_tokens, 20000),
        )
        contexts[scope_key(project, branch)] = context
        return json.dumps(context, default=str)

    @mcp.tool()
    def get_evidence(evidence_id: str, branch: str = "main", project: str = "kubernaut") -> str:
        """Return one evidence item from the most recent triage dossier."""
        context = contexts.get(scope_key(project, branch))
        if not context:
            return json.dumps({"error": "triage_test_failure must be called first"})
        for item in context["evidence"]:
            if item["id"] == evidence_id:
                return json.dumps(item, default=str)
        return json.dumps({"error": f"unknown evidence id: {evidence_id}"})

    @mcp.tool()
    def get_related_events(evidence_id: str, branch: str = "main", project: str = "kubernaut") -> str:
        """Return timeline records related to one evidence item."""
        context = contexts.get(scope_key(project, branch))
        if not context:
            return json.dumps({"error": "triage_test_failure must be called first"})
        related = [item for item in context["timeline"] if item["evidence_id"] == evidence_id]
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
