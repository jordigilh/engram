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
- stdio backend adapter via the official `rmcp` client: one long-lived child
  per adapter, a minimal child environment, serialized calls, one restart/retry
  for read tools (writes surface `outcome_unknown` instead of replaying),
  bounded tool pagination, `shared_key` reuse across projects, catalog prewarm,
  and `rmcp`'s graceful stdio shutdown. `timeout_seconds` is parsed for config
  compatibility but not enforced for stdio (Python parity).
- Catalog aggregation and filtering: backend prefixes, collision
  qualification, per-backend allowlists, `search_for_pattern` alias, managed
  zvec freshness arguments.
- Hindsight scope enforcement, gateway-owned manual mental-model writer, and
  automatic `docs_manual`/`issues_manual` synthesis.
- Best-effort concurrent catalog prewarm after the listener binds; `tools/list`
  refreshes and replaces the cache, `tools/call` routes from it.
- Typed `-32002` envelopes: upstream `workspace_unbound` (409) and
  `workspace_flushing` (503 + `Retry-After`) are relayed unchanged;
  `outcome_unknown` (409, non-retryable) is gateway-owned for
  transport-uncertain writes. `workspace_auth_timeout` is defined for future
  authorization paths.
- Freshness envelope `_meta.engram_freshness` on `tools/list` and successful
  `tools/call` results: project, workspace, catalog generation/state, refresh
  time, and per-backend errors.
- Bounded session state: 30-minute idle TTL, 256 sessions per project with
  oldest-idle eviction, a global cap, one-shot cancellation, and random ids.

Deliberately not implemented in this slice:

- `shadow_http` adapter. Registry parsing accepts it for config compatibility,
  but adapter construction rejects it with a startup error. `shadow_http` is a
  CocoIndex-comparison-only path and is expected to retire with #214.
- Serena-specific `search_for_pattern` overflow normalization (the zvec alias
  is implemented; no live config exposes raw Serena pattern search).
- SCIP reads (#212), edit/diagnostics (#213), ingestion (#214), language
  adapters (#215).
- Any change to the Python gateway. `src/engram/pipeline/engram_gateway.py`
  stays runnable and authoritative.

## Hindsight proxy (`engram-proxy`)

`crates/engram-proxy` is the native, byte-transparent TCP proxy that owns the
public Hindsight port and fronts the blue/green `hindsight-api` pair. It is a
direct replacement for `src/engram/pipeline/hindsight_proxy.py` and performs no
HTTP/MCP parsing or header rewriting. The Python proxy remains authoritative
until an operator opts in.

Contract:

- Listener defaults to `0.0.0.0:8888` (`HINDSIGHT_PROXY_HOST`,
  `HINDSIGHT_PROXY_PORT`).
- The backend is always `127.0.0.1`; the port is re-read from
  `~/.engram/state/active-backend.port`
  (`HINDSIGHT_ACTIVE_BACKEND_FILE`) for **every new client connection**.
- A missing, unreadable, empty, or invalid state file falls back to
  `18888` (`HINDSIGHT_DEFAULT_BACKEND_PORT`).
- Backend connect timeout is 5 seconds; refusal/timeout closes only that
  client.
- Existing connections stay pinned to the backend they resolved at accept
  time, so a blue/green swap is invisible to them.
- The accept loop spawns one relay per connection; a hung backend cannot stall
  the listener. Accept errors retry with bounded backoff; the listener never
  exits on its own.
- Both sockets use `TCP_NODELAY`, matching asyncio's transport defaults.

Intentional differences from the Python reference (all documented and tested):

- Only the first address returned by the listener host lookup is bound;
  Python binds every result. The default `0.0.0.0` is unaffected.
- State values `0`, negative, `>65535`, and underscore forms fall back to the
  default instead of being passed to `connect`; `+18888` is accepted.
- Missing `HOME` with no explicit `HINDSIGHT_ACTIVE_BACKEND_FILE` is a startup
  error.
- There is no idle timeout: Cursor keeps persistent HTTP connections open
  indefinitely. A relay closes both sides as soon as either direction reaches
  EOF or errors, matching the Python `_pump`, so a half-closed client cannot
  pin a relay task.

Run (from `rust/`):

```bash
cargo run -p engram-proxy
# or, after build, use the absolute path in service definitions:
./target/release/engram-proxy
```

The binary prints one `PROXY_LISTEN=<addr>` line on stdout before serving.

### Opt-in and rollback

The existing launchd/systemd unit keeps running the Python proxy. To evaluate
the Rust proxy, run it on an alternate listener and a temporary state file:

```bash
HINDSIGHT_PROXY_PORT=8889 \
HINDSIGHT_ACTIVE_BACKEND_FILE="$HOME/.engram/state/active-backend.port" \
  /absolute/path/to/rust/target/release/engram-proxy
```

Smoke test before switching:

1. Compare loopback retain and recall through `127.0.0.1:8889` with the same
   operations against Hindsight directly on the active blue/green port.
2. Hold a long-lived connection through the proxy while
   `hindsight-blue-green-restart.sh` performs a swap; the connection must keep
   working and new connections must use the new port.
3. Check resident memory with a representative number of idle connections
   (relay tasks are unbounded, matching Python).

Rollback: stop the `engram-proxy` process and leave the existing Python proxy
unit untouched. Path precedence matters for manual runs: `~/.cargo/bin` may
shadow the venv's `engram-hindsight-proxy` console script, so always invoke
`./target/release/engram-proxy` by absolute path. A dedicated opt-in launchd
plist is deferred to the deployment follow-up.

Tests: `cargo test -p engram-proxy --locked` covers state-file parsing and
fallback, byte transparency, concurrent clients, pinned blue/green switching,
backend refusal/timeout, listener responsiveness under a hung client, and the
documented environment contract via the real binary.

## Run

```bash
cd rust
cargo test --all
cargo run -p engram-mcp -- --config ~/.engram/runtime/native-instances.toml --port 8898
```

Without `--config`, the server uses `~/.engram/runtime/native-instances.toml`
when it exists, or named static routes from `--projects`. The server binds
loopback only and refuses a non-loopback `--host`. The default is stateless,
matching the Python gateway: non-`initialize` requests work without a session.
Add `--require-session` for strict MCP behavior (the hidden
`--allow-stateless` flag remains accepted for installed units). In stateless
mode, `DELETE` and cancellation fencing apply only to requests that carry
`Mcp-Session-Id`.

## Python parity harness

`tests/fixtures/parity/` holds input/expected pairs generated from the Python
gateway (`src/engram/pipeline/engram_gateway.py`) with
`tests/fixtures/parity/generate.py`. `tests/parity_shaping.rs` compares the
Rust shaping functions against those files, and `tests/gateway_routing.rs`
checks that the HTTP gateway applies them on the real request path.

Current parity coverage (issue #224 Phase 0+2):

- Hindsight `recall` results are normalized to the bounded
  `engram-recall.v1` text + `structuredContent` shape (8 results, 800-char
  summaries, 240-char metadata values, keywords, rounded scores), including
  legacy `*_recall` names.
- `search_for_pattern` exposes `max_answer_chars` and truncates overlong
  results with the Python suffix and boundary behavior.

Phase 2 parity (transport): workspace-routed calls without `?workspace` are
forwarded like Python; catalog refreshes never reject a `tools/call` with 503;
freshness metadata reports `catalog_state: refreshing`; the runtime defaults to
stateless sessions.

Intentional divergences: Rust returns `{}` for `ping` and JSON-RPC `-32601` for
unknown methods, while Python returns a minimal initialize-shaped success for
any request id. Rust also refuses non-loopback binds and Host/Origin values.

Still deferred from #224: the shadow adapter, call metrics, the copy of the
full Python gateway suite, and Serena's `search_for_pattern` overflow shaping
(raw Serena backend; no live config exposes it, tracked with the Serena
retirement slice).

## Contract tests

- `tests/lifecycle.rs`: initialize, notifications, tools/list isolation,
  DELETE, cancellation, Origin/Host validation, transport errors.
- `tests/stdio_adapter.rs`: real stdio child via `rmcp`, restart, pagination,
  old-protocol negotiation, server-to-client requests, cancellation,
  malformed output, and gateway routing.
- `tests/gateway_routing.rs`: real HTTP adapter against a mock MCP server
  (session sequence, retry matrix, workspace), catalog aggregation, prefix
  routing, search alias, zvec freshness, Hindsight scope, manual writer,
  prewarm, catalog replacement, legacy host.
- Unit tests: JSON-RPC shapes, catalog filters, registry validation, session
  store, adapter parsing.

## Stacked follow-ups

1. #211 follow-up: shadow adapter if still required.
2. #212: Go SCIP read path with epoch admission.
3. #213: edit/diagnostics layer and Serena retirement gate.
4. #214: Rust ingestion and Cocoindex retirement.
5. #215: additional language adapters.
