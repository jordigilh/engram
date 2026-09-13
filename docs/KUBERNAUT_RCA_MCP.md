# Kubernaut RCA MCP

The RCA MCP server is **Kubernaut-specific**. It is not exposed to other Engram
projects or families.

## Endpoint

```text
http://127.0.0.1:8897/mcp
```

The Engram gateway adds the RCA backend only for these project identities:

- `kubernaut`
- `kubernaut-operator`
- `kubernaut-v1.5`
- `kubernaut-v1.6`

Other projects, including `kubernaut-console`, do not receive the RCA MCP entry.

## Branch Scope

Every RCA operation is scoped to a target branch:

```text
main
release/v1.5
release/v1.6
```

The service normalizes `release/v1.5` to `v1.5`. Branch scope is included in
MCP state, failure-family signatures, retained incident IDs, and historical
queries. Evidence from `main` cannot be returned for a `release/v1.5` request.

## Tools

### `ingest_test_run`

Downloads a GitHub Actions job log and must-gather artifact into temporary
storage, normalizes the evidence, and indexes it for the requested scope.

Required arguments:

```text
test_log_url
must_gather_url
branch
```

Optional metadata:

```text
project
commit_sha
repository
workflow
```

Only GitHub Actions job and artifact URLs are accepted. Authentication uses
`GH_TOKEN` or `GITHUB_TOKEN`.

### `triage_test_failure`

Builds a bounded dossier using exact RR-ID correlation, typed Kubernetes
evidence, timestamps, error clusters, and provenance.

Required arguments:

```text
run_id
job_id
test_name
failure_text
branch
```

Optional arguments:

```text
rr_id
project
max_tokens
```

### `generate_rca`

One-shot on-demand RCA for a CI run whose `rca-dossier` CI job never
produced an artifact -- e.g. a timed-out or cancelled job such as
`kubernaut/actions/runs/34236030006/job/102110076210`, which hits the job
execution cap with no `[FAILED]` block and therefore skips the CI dossier
phase entirely.

Pass `run_id` with an optional `job_id` (a run/job page URL pasted as
`test_log_url` also works -- query parameters such as `?pr=2379` are
tolerated and the IDs are parsed out of it) and the tool discovers the job
log and must-gather artifact via the GitHub API, then extracts failures and
builds bounded dossiers in the same deterministic pipeline the batch
backfill uses. Explicit `test_log_url`/`must_gather_url` pin either side
and skip discovery for that side; `artifact_hint` narrows artifact
selection when a run uploads several.

A missing or expired must-gather artifact degrades to a log-only RCA
instead of failing. When no RR-linked failure exists (the timeout case),
the tool returns a `degraded` analysis -- timeout/cancel detection, job-log
tail, failure manifest, must-gather evidence summary, and suggested next
steps -- alongside empty `dossiers`, so upstream still gets something
actionable. `max_dossiers` (default 3, capped at 5) bounds how many
dossiers come back; the first dossier also becomes the scope's current
triage context, so `get_evidence`/`get_related_events` keep working
afterwards, searching the remaining dossiers on a miss.

### `get_evidence`

Returns one evidence item from the most recent triage context for the requested
project and branch.

### `get_related_events`

Returns timeline records related to an evidence ID for the requested scope.

### `promote_incident`

Optionally persists a compact dossier and its change relationships. This is not
used by the stateless CI dossier phase.

### `get_failure_history`

Returns promoted incidents for a branch-scoped failure family.

### `get_incident_timeline`

Returns one promoted incident and its linked changes. The incident ID must
belong to the requested project and branch.

## Typical Agent Flow

```text
ingest_test_run(branch="main", ...)
  -> triage_test_failure(branch="main", ...)
  -> get_evidence(branch="main", ...)
  -> get_related_events(branch="main", ...)
```

On-demand flow when no CI dossier artifact exists for the run:

```text
generate_rca(run_id="34236030006", job_id="102110076210", branch="main")
  -> get_evidence(branch="main", ...)   # searches all returned dossiers
  -> get_related_events(branch="main", ...)
```

For release-line work, replace `main` with the matching release scope. Do not
use a generic branch name such as a feature branch; feature branches are
treated as the target line they are based on.

## OpenCode Configuration

OpenCode connects to the single frontend gateway entry:

```json
{
  "mcp": {
    "engram": {
      "type": "remote",
      "url": "http://127.0.0.1:8896/mcp/kubernaut",
      "enabled": true
    }
  }
}
```

The gateway aggregates the normal Kubernaut tools and the RCA backend behind
this one connection. RCA is not added as a separate OpenCode MCP connection;
this preserves the heartbeat/degradation behavior of the existing frontend
proxy.

The CI dossier job does not call MCP. It generates and uploads a compact dossier
as an artifact. MCP is the interactive agent-facing path for re-ingestion and
evidence retrieval.
