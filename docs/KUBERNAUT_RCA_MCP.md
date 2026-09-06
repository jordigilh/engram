# Kubernaut RCA MCP

The RCA MCP server is **Kubernaut-specific**. It is not exposed to other Engram
projects or families.

## Endpoint

```text
http://127.0.0.1:8897/mcp
```

The OpenCode plugin adds the `kubernaut-rca` server only for these project
identities:

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

For release-line work, replace `main` with the matching release scope. Do not
use a generic branch name such as a feature branch; feature branches are
treated as the target line they are based on.

## OpenCode Configuration

The OpenCode plugin derives the project identity from the checkout and current
branch. It conditionally injects the RCA MCP server; no manual MCP entry is
needed in each Kubernaut repository.

The CI dossier job does not call MCP. It generates and uploads a compact dossier
as an artifact. MCP is the interactive agent-facing path for re-ingestion and
evidence retrieval.
