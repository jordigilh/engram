# Latest-State Views

`engram-latest-state` exposes `query_latest_state_view`, a deterministic MCP
tool for materializing the latest valid state of known entities. It does not
call `recall`, `reflect`, or an LLM. Historical records remain in the source
bank; only the response is latest-state-oriented.

## Contract

The server owns view definitions and scopes. Callers select them by name:

```text
query_latest_state_view(
  view_id="workstream-status",
  scope="northstar",
  as_of="2026-09-16T12:00:00Z",
  include_provenance=true
)
```

The view definition describes entity identity, projected fields, optional
source precedence, and stale-data policy. A view can be supplied to the
server with `--view-config`; the caller cannot provide or broaden a view at
query time.

Example view definition:

```json
{
  "view_id": "workstream-status",
  "entity_paths": ["metadata.entity_key"],
  "fields": {
    "status": {
      "paths": ["metadata.status", "metadata.state"],
      "source_precedence": ["verification", "tracker"]
    },
    "owner": {
      "paths": ["metadata.owner"]
    }
  },
  "stale_after_seconds": 86400
}
```

For sources without one canonical entity field, `entity_key_options` can
declare ordered composite alternatives, for example:

```json
"entity_key_options": [
  ["metadata.project", "metadata.key"],
  ["metadata.repo", "metadata.kind", "metadata.number"]
]
```

The first complete option wins. This prevents issue or ticket numbers from
colliding across repositories or projects.

## Reduction Rules

- Invalidated, deleted, and superseded records are ignored.
- Records newer than an explicit `as_of` are ignored.
- Entity fields are reduced independently, not as whole documents.
- The newest observation wins for each field.
- Equal-time observations use declared source precedence when available.
- Equal-time observations with different values and equal precedence remain an
  explicit conflict; no value is invented.
- Missing fields are returned as missing evidence.
- Provenance includes memory ID, document ID, source, and observation time when
  requested.
- Repeated queries over the same input snapshot and `as_of` produce the same
  ordering and values.

## Running Locally

```bash
engram-latest-state \
  --bank-id example-issues \
  --view-config ./views.json \
  --scope-name northstar \
  --scope-tag northstar \
  --transport stdio
```

The server reads only `GET /v1/default/banks/{bank}/memories/list`, paginating
with a bounded scan. Scan truncation and the reported total are included in
the response freshness metadata.
