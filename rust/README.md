# Native Engram MCP runtime

This Cargo workspace implements issue
[#211](https://github.com/jordigilh/engram/issues/211) slices 1 and 2 in the
`engram-mcp` crate: MCP lifecycle plus HTTP backend routing and catalog parity.
The Python gateway remains runnable and authoritative during migration.
Rust 1.88+ is required (the HTTP client stack's ICU dependencies).

## Scope

Implemented:

- MCP lifecycle: `initialize`, notifications, `tools/list`, `DELETE`,
  cancellation, session isolation, bounded sessions, Origin/Host validation.
- Registry loading from `instances.toml`: HTTP backends, legacy `endpoint`,
  full validation, insertion order preserved.
- HTTP backend adapter: one-shot MCP sessions, JSON/SSE parsing, read-only
  retry policy, workspace query forwarding.
- Catalog aggregation and filtering: backend prefixes, collision
  qualification, per-backend allowlists, `search_for_pattern` alias, managed
  zvec freshness arguments.
- Hindsight scope enforcement, gateway-owned manual mental-model writer, and
  automatic `docs_manual`/`issues_manual` synthesis.
- Best-effort concurrent catalog prewarm after the listener binds; `tools/list`
  refreshes and replaces the cache, `tools/call` routes from it.
- Typed `-32002` envelopes: `workspace_unbound` (409), `workspace_flushing`
  (503 + `Retry-After`), and `outcome_unknown` (409, non-retryable) for
  transport-uncertain writes. `workspace_auth_timeout` is defined for future
  authorization paths.
- Freshness envelope `_meta.engram_freshness` on `tools/list` and successful
  `tools/call` results: project, workspace, catalog generation/state, refresh
  time, and per-backend errors.
- Bounded session state: 30-minute idle TTL, 256 sessions per project with
  oldest-idle eviction, a global cap, one-shot cancellation, and random ids.

Deliberately not implemented in this slice:

- Stdio and `shadow_http` adapters. Registry parsing accepts them for config
  compatibility, but adapter construction rejects them with a startup error.
- Response shaping (recall/Serena normalization, `max_answer_chars`
  truncation).
- SCIP reads (#212), edit/diagnostics (#213), ingestion (#214), language
  adapters (#215).
- Any change to the Python gateway. `src/engram/pipeline/engram_gateway.py`
  stays runnable and authoritative.

## Run

```bash
cd rust
cargo test --all
cargo run -p engram-mcp -- --config ~/.engram/runtime/native-instances.toml --port 8898
```

Without `--config`, the server uses `~/.engram/runtime/native-instances.toml`
when it exists, or named static routes from `--projects`. The server binds
loopback only and refuses a non-loopback `--host`. Add
`--allow-stateless` to accept non-`initialize` requests without a session.
In that migration mode, `DELETE` and cancellation fencing apply only to
requests that carry `Mcp-Session-Id`.

## Contract tests

- `tests/lifecycle.rs`: initialize, notifications, tools/list isolation,
  DELETE, cancellation, Origin/Host validation, transport errors.
- `tests/gateway_routing.rs`: real HTTP adapter against a mock MCP server
  (session sequence, retry matrix, workspace), catalog aggregation, prefix
  routing, search alias, zvec freshness, Hindsight scope, manual writer,
  prewarm, catalog replacement, legacy host.
- Unit tests: JSON-RPC shapes, catalog filters, registry validation, session
  store, adapter parsing.

## Stacked follow-ups

1. #211 follow-up: stdio and shadow adapters if still required.
2. #212: Go SCIP read path with epoch admission.
3. #213: edit/diagnostics layer and Serena retirement gate.
4. #214: Rust ingestion and Cocoindex retirement.
5. #215: additional language adapters.
