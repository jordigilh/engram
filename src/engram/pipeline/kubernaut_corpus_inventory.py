"""Inventory failed Kubernaut CI runs before downloading any evidence."""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MUST_GATHER_NAME_RE = re.compile(r"must.?gather|fullpipeline|coverage-e2e-fullpipeline", re.IGNORECASE)
DOWNSTREAM_JOB_RE = re.compile(r"summary|merge.?gate|report", re.IGNORECASE)


@dataclass(frozen=True)
class GitHubActionsClient:
    repository: str
    token: str
    request: Callable[[str, dict[str, str]], dict[str, Any]] | None = None

    def get(self, path: str, **query: int) -> dict[str, Any]:
        if self.request:
            return self.request(path, {key: str(value) for key, value in query.items()})
        params = urllib.parse.urlencode(query)
        url = f"https://api.github.com/repos/{self.repository}/{path}"
        if params:
            url += f"?{params}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "engram-kubernaut-corpus-inventory",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def failed_runs(self, limit: int) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        page = 1
        while len(runs) < limit:
            response = self.get("actions/runs", per_page=100, page=page)
            batch = [run for run in response.get("workflow_runs", []) if run.get("conclusion") == "failure"]
            runs.extend(batch)
            if len(response.get("workflow_runs", [])) < 100:
                break
            page += 1
        return runs[:limit]

    def inventory_run(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id = int(run["id"])
        jobs = self.get(f"actions/runs/{run_id}/jobs", per_page=100).get("jobs", [])
        artifacts = self.get(f"actions/runs/{run_id}/artifacts", per_page=100).get("artifacts", [])
        failed_jobs = [
            {
                "job_id": job["id"],
                "name": job.get("name"),
                "conclusion": job.get("conclusion"),
                "started_at": job.get("started_at"),
                "completed_at": job.get("completed_at"),
                "log_url": f"https://github.com/{self.repository}/actions/runs/{run_id}/job/{job['id']}",
            }
            for job in jobs
            if job.get("conclusion") == "failure"
        ]
        artifact_records = [
            {
                "artifact_id": artifact["id"],
                "name": artifact.get("name"),
                "expired": artifact.get("expired", False),
                "size_in_bytes": artifact.get("size_in_bytes", 0),
                "created_at": artifact.get("created_at"),
                "url": f"https://github.com/{self.repository}/actions/runs/{run_id}/artifacts/{artifact['id']}",
            }
            for artifact in artifacts
        ]
        relevant_artifacts = [artifact for artifact in artifact_records if MUST_GATHER_NAME_RE.search(artifact["name"] or "")]
        primary_failed_jobs = [job for job in failed_jobs if not DOWNSTREAM_JOB_RE.search(job["name"] or "")]
        return {
            "run_id": run_id,
            "run_number": run.get("run_number"),
            "repository": self.repository,
            "workflow": run.get("name") or run.get("workflow_name"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "commit_sha": run.get("head_sha"),
            "run_url": run.get("html_url"),
            "failed_jobs": failed_jobs,
            "primary_failed_jobs": primary_failed_jobs,
            "artifacts": artifact_records,
            "relevant_artifacts": relevant_artifacts,
            "usable_for_dossier": bool(primary_failed_jobs and relevant_artifacts),
        }

    def inventory(self, limit: int) -> dict[str, Any]:
        runs = [self.inventory_run(run) for run in self.failed_runs(limit)]
        usable = [run for run in runs if run["usable_for_dossier"]]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "repository": self.repository,
            "requested_failed_runs": limit,
            "failed_runs_scanned": len(runs),
            "usable_runs": len(usable),
            "runs": runs,
        }

    def inventory_run_id(self, run_id: int) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "repository": self.repository,
            "requested_failed_runs": 1,
            "failed_runs_scanned": 1,
            "usable_runs": 1,
            "runs": [self.inventory_run(self.get(f"actions/runs/{run_id}") )],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="jordigilh/kubernaut")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")
    client = GitHubActionsClient(args.repo, token)
    inventory = client.inventory_run_id(args.run_id) if args.run_id else client.inventory(args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(inventory, indent=2) + "\n")
    print(json.dumps({key: inventory[key] for key in ("repository", "failed_runs_scanned", "usable_runs")}))


if __name__ == "__main__":
    main()
