# Native Engram MCP runtime

This Cargo workspace is the foundation for issue
[#211](https://github.com/jordigilh/engram/issues/211). The first stacked PR
contains **only** the `engram-mcp` crate and its lifecycle contract.

## Scope of this slice

Implemented:

- `initialize` with the Python gateway's protocol version, capabilities, and
  `serverInfo` shape.
- `Mcp-Session-Id` issuance and `DELETE` termination.
- `notifications/initialized` and notification `202` responses.
- `notifications/cancelled` with request-id fencing (`-32800`).
- `tools/list` with project-scoped catalogs.
- Per-project session isolation: a session is valid only on the route that
  created it.
- Bounded session state: 30-minute idle TTL, 256 sessions per project with
  oldest-idle eviction, a global cap, one-shot cancellation, and random ids.
- `Origin` and loopback `Host` validation for browser-originated requests:
  loopback origins and an explicit `--allow-origin` allowlist pass; requests
  without `Origin` (native MCP clients) remain allowed.
- `ping` returns `{}`; unknown methods return `-32601` instead of a fake
  initialize result.
- Re-`initialize` with a valid session reuses it rather than creating an
  orphan.
- Non-`initialize` requests require `Mcp-Session-Id` by default. Use
  `--allow-stateless` only for Python-gateway parity during migration.
- A supplied session header is validated on every request, including
  `initialize`; terminated or cross-project sessions return 404.
- Cancellation notifications must be versioned and id-less; malformed
  envelopes are rejected without changing cancellation state.

Deliberately not implemented in this slice:

- Backend routing and `tools/call` dispatch (#211 slice 2).
- SCIP reads (#212), edit/diagnostics (#213), ingestion (#214), language
  adapters (#215).
- Any change to the Python gateway. `src/engram/pipeline/engram_gateway.py`
  stays runnable and authoritative.

## Run

```bash
cd rust
cargo test --all
cargo run -p engram-mcp -- --projects engram,kubernaut --port 8898
```

The server binds loopback only and refuses a non-loopback `--host`. Add
`--allow-stateless` to accept non-`initialize` requests without a session.
In that migration mode, `DELETE` and cancellation fencing apply only to
requests that carry `Mcp-Session-Id`; header-less traffic stays stateless.

## Contract tests

`crates/engram-mcp/tests/lifecycle.rs` covers initialize, notifications,
tools/list isolation, DELETE, cancellation, cross-project session rejection,
Origin validation, transport errors, and stateless compatibility. Unit tests
cover the JSON-RPC payload shapes and the session store.

## Stacked follow-ups

1. #211 slice 2: gateway backend routing and contract parity with the Python
   aggregation path.
2. #212: Go SCIP read path with epoch admission.
3. #213: edit/diagnostics layer and Serena retirement gate.
4. #214: Rust ingestion and Cocoindex retirement.
5. #215: additional language adapters.
