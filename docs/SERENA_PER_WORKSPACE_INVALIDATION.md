# Serena per-workspace invalidation (issue #204)

**Status:** Design approved; Phase 1 mechanism implemented, topology/barrier follow-ups pending
**Date:** 2026-11-20
**Issue:** [jordigilh/engram#204](https://github.com/jordigilh/engram/issues/204)
**Related:** `docs/NATIVE_ENGRAM_DESIGN.md` (Stage 2),
`src/engram/pipeline/serena_multiplex.py`, `git-hooks/family/`,
zvec-grep `crates/zg-daemon/src/workspace_runtime.rs`

## 1. Problem

The kubernaut-family deployment runs one shared Serena daemon (8892) with a
single process-global "active project" LSP state. Git operations rewrite files
under that state; gopls does not reliably observe every mutation in this
worktree-heavy setup, so the daemon can stay live and responsive while serving
pre-checkout symbols. That is worse than an outage: `find_symbol` and
`find_referencing_symbols` return plausible, wrong answers.

Today's mitigation is a debounced git hook that restarts the whole daemon after
ref-changing operations. It guarantees freshness but produces a family-wide
failure window (ReadTimeout, then 503), so a commit in one worktree interrupts
every workspace.

## 2. Goals and non-goals

### Goals

1. A mutation in one workspace never interrupts another workspace's queries.
2. A query that may serve pre-mutation state is discarded, never answered.
3. Callers get a distinguishable "flushing, retry" response, not a timeout or
   generic 503.
4. The family-wide restart hook stops being the correctness mechanism.

### Non-goals

- Reimplementing Serena/gopls. Serena remains the exact-symbol authority.
- Changing Serena's tool wire protocol; only the multiplex busy contract
  changes.
- Shipping deployment-local plists, registries, symlinks, or `~/.engram`
  scripts. Engram ships mechanisms; the deployment owns its config (recorded
  architecture decision).

## 3. Design

### 3.1 Per-workspace ownership

State is owned per canonical workspace path (`WorkspaceKey = realpath`), not per
downstream session and not per process.

```
WorkspaceState {
  epoch: uint64            # bumped on every observed mutation
  activated_epoch: uint64  # epoch at which the LSP was last proven fresh
  state: ready | flushing
  runtime_generation: uint64
  last_observed_head: string
}
```

**Phase 1 (this repository) runs one Serena process/endpoint per
`WorkspaceKey`**, lazily spawned and idle-evicted. Eviction bumps
`runtime_generation` and clears readiness, so a fresh process must be re-proven
before serving. A separate transport session inside one shared process is not
enough: Serena's `active_project` is process-global. Startup rejects two
workspaces mapped to the same endpoint.

The transitional `--upstream-mode=shared` fallback keeps one process and offers
typed retries only; it is **migration-only**, does not claim acceptance
criterion 1, and is removed once `enforce` has been stable for one release.
Phase 2 (Stage 2 native runtime) preserves the same per-workspace contract.

### 3.2 Mutation detection

The authoritative signal is explicit, and delivery is durable:

- Git hooks (`post-checkout`, `post-merge`, `post-rewrite`,
  `reference-transaction`) resolve the worktree top level and emit a versioned
  event: `{version, workspace, mutation_id, transaction_id,
  phase: begin|ref_committed|worktree_completed|aborted}`.
- Events go to a durable spool (atomic write) and are POSTed to the multiplex.
  A failed POST leaves the spool file.
- The multiplex drains the spool on admission and startup. Each workspace also
  runs an unthrottled per-worktree probe of the resolved HEAD object ID, index,
  and checked-out ref lock. This covers a lost hook and the pre-ref-update
  phase of a checkout. It never probes `--git-common-dir`, so sibling
  worktrees are isolated.
- The probe has a hard deadline (default 1 s) and is single-flight per
  workspace; a timeout returns typed flushing rather than holding the call gate
  indefinitely.
- Deduplication is keyed by `(transaction_id, phase, mutation_id)`, so a
  `begin` never suppresses its own terminal event.

Events for unknown or unauthorized workspaces are rejected; they never fall
back to "invalidate everything".

### 3.3 Epoch and query admission

Every scoped `tools/call`:

1. Resolves its bound workspace. Binding is mandatory (`--require-workspace`
   `off|warn|enforce`; only `enforce` rejects unbound calls). `warn` is the
   migration shadow: log and forward legacy-style, without a freshness claim.
2. Acquires the per-workspace FIFO call gate (all calls, including healthy
   ones, are serialized per workspace; a waiter times out as typed flushing).
   No separate upstream lock is needed in Phase 1.
3. Runs the probe outside the state lock, then under a short state lock applies
   the result, recaptures `start_epoch`/`start_generation`, and skips a restart
   if another caller already made this epoch ready.
4. If `flushing` or `activated_epoch != start_epoch`: activate the workspace if
   needed, call `restart_language_server`, run the settle proof, and CAS
   `ready`/`activated_epoch` only if the epoch and runtime generation are
   unchanged. The **whole activation path is bounded** by
   `ACTIVATION_DEADLINE_S` (default 20 s, below typical client timeouts); on
   expiry, return typed `workspace_flushing` (activation may continue in the
   background) rather than letting the caller observe ReadTimeout. A failed
   restart, failed settle, or lost CAS also aborts with typed flushing and
   never forwards.
5. Forwards while holding the call gate. **For a write, acquire the shared
   git-barrier `flock`, re-run the probe, and re-check the epoch/state
   immediately before dispatch** (see §7, "Write barrier"), so a checkout cannot
   start between the check and the write. A read whose post-call probe or state check fails
   is discarded and returns typed flushing. A write whose epoch or runtime
   generation changed returns `outcome_unknown` and is never replayed.
   Invalidation takes only the short state lock, so it never waits behind I/O.

**Tool routing in per-workspace mode:** intercept `activate_project`; reject
`query_project`/`list_queryable_projects` or require them to name a registered
workspace and fence that workspace. They are never forwarded untouched, because
they can flip this process's active project and reintroduce the wrong-repo race.

**Authorization timeouts are not denials (2026-11-21 incident).** An exact
allowlisted root is accepted without a git subprocess. A linked-worktree check
that times out returns the typed `workspace_auth_timeout` (retryable), never
`403 workspace is outside the allowlist`. A 403 means the path is genuinely
unauthorized.

**Retry safety:** only tools on an explicit read-only allowlist are retried.
Writes (and unknown tools) are never replayed; transport loss, session loss, or
an epoch change mid-write returns `outcome_unknown`. Startup fails closed on an
unknown tool if a capability registry is enabled.

### 3.4 Typed "flushing, retry" contract

```
HTTP/1.1 503 Service Unavailable
Retry-After: 2
X-Engram-Error: workspace_flushing
```

```json
{"jsonrpc":"2.0","id":42,"error":{"code":-32002,
 "message":"workspace_flushing: retry in 2s",
 "data":{"kind":"workspace_flushing","workspace":"/abs/path",
         "epoch":17,"retry_after_ms":2000}}}
```

Kinds: `workspace_flushing` (retry), `workspace_unbound` (fix request),
`workspace_auth_timeout` (retry; the authorization check itself timed out),
`outcome_unknown` (reconcile, never retry). A real upstream failure keeps the
existing generic 503 and is not labeled flushing. Read notifications get 202;
mutating notifications are rejected at the HTTP level (409, header-only), since
a JSON-RPC notification has no id to carry the error.

**Trust boundary:** the multiplex is single-user and loopback-only. The admin
endpoints require a token read from a deployment file with restricted
permissions; non-loopback binds are refused. The registry is routing, not caller
authentication; multi-user deployments must front `/mcp` with an authenticating
proxy.

### 3.5 Hook retirement

Ship `git-hooks/family/_invalidate-family-workspaces.sh.tmpl` and updated
post-hook templates. `generate-hooks.sh` gains `HOOK_VARIANT=invalidate|restart`
and invalidation keys (URL, token file, spool dir). Variant-suffixed filenames
let both sets coexist; relinking is the switch.

Migration: shadow with `--require-workspace=warn` and the old restart hook still
active; confirm the client inventory and zero new unbound clients; relink to the
invalidation hooks and confirm the Serena PID stays stable across a commit
burst; flip `enforce`; keep the legacy restart script one release for rollback.
**In `warn` mode, bound calls already receive full epoch/probe admission; only
rejection and freshness fencing of unbound calls are deferred.** Relinking
therefore never removes the fence from bound calls. Require the shadow window
to cover a representative usage period, not just any single call.

Rollback also has a multiplex-side lever: `--upstream-mode=shared` plus relinking
the restart variant. On rollback, drain or archive the spool and reset workspace
epochs/readiness, so stale `git_active` state cannot leak into the legacy path.

### 3.6 Invalidation primitive and readiness

`activate_project` is not an invalidation primitive: same-project activation is
a no-op in current Serena (`CHANGELOG` #1280). The primitive is the optional
`restart_language_server` tool. Startup fails closed if invalidation is enabled
and the tool is absent.

After the restart, a bounded readiness probe must prove the **current
revision**: read one registered file and match its hash to disk, look up a
configured symbol (or a symbols overview), and require the resolved HEAD to be
unchanged. A successful restart alone does not mark the workspace `ready`.

**Request-level liveness (2026-11-21 incident).** A hung Serena child can keep
its TCP listener open while never answering, so process-exit supervision
(KeepAlive, the supervisor) cannot detect it. Every spawn and every cached
process reuse therefore performs a bounded MCP `initialize` request; a timeout
kills the process group and respawns. A successful TCP connect is not liveness.
The same bounded request applies after `restart_language_server`, so a hung LSP
session is recycled like a stale one.

### 3.7 Serena version compatibility (validated against 2.1.0)

- `mcp-session-id` header flow is unchanged; the downstream-session model
  survives.
- `activate_project` exists only in multi-project mode. A shared daemon must
  **not** launch with `--project`; per-workspace processes may, and then do not
  need activation.
- Serena 2.1.0 requires an internal `session` argument on `activate_project`
  (renamed from `session_id`). The multiplex obtains it by calling
  `initial_instructions` once per upstream session and parsing
  `Your Serena session id is '<id>'`, then sends
  `{"project": ..., "session": <id>}`.
- **Target pin:** an unreleased main / 2.2.0 build, not 2.1.0. The
  SIGTERM symbol-cache preservation and stdin-failure hang fixes are not in
  2.1.0.
- **License boundary:** Serena remains Apache-2.0. Engram talks to it only over
  MCP/HTTP as a separate process; it must never import Serena in-process.

## 4. Acceptance criteria mapping
| Criterion | Mechanism |
|---|---|
| Commit in one worktree does not interrupt others | One process/endpoint per workspace; invalidation touches only its runtime (3.1, 3.3) |
| Queries during re-activation return typed retryable response | `workspace_flushing` contract (3.4) |
| No silently stale results after checkout/commit | Epoch fence + per-worktree probe + restart + revision-correlated readiness (3.2, 3.3, 3.6) |
| Family-wide restart hook retired | Dual-variant generator, staged relink, restart script demoted to rollback (3.5) |

## 5. Verification

| Acceptance criterion | Named test |
|---|---|
| No cross-workspace interruption | Commit burst in A while querying B: B never errors/times out |
| Typed retryable response | Flushing response carries `-32002`, `Retry-After`, `X-Engram-Error` |
| No silently stale results | Lost-hook/checkout probe catch-up, CAS abort on epoch bump, post-call discard |
| Hung but listening Serena | MCP initialize timeout recycles the process |
| Auth check timeout | `workspace_auth_timeout`, not a 403 denial |
| Hook retired | Rendered dual-variant hooks; launchd PID stable across a commit burst; rollback relink |

Additional adversarial tests: write-vs-checkout interleaving (barrier), duplicate
and reordered phase events, unknown tool fail-closed.

## 6. Open questions

1. Confirm `restart_language_server` is exposed in the deployed Serena context;
   if not, enable it in deployment config (startup already fails closed).
2. Is `503 + Retry-After + typed body` acceptable, or should flushing use
   `429`/`425`?

## 7. Implementation considerations (hardening, not core design)

These are correctness details for implementation; they do not expand the issue's
scope:

- **Write barrier:** git hooks and writes share a per-workspace `flock`; writes
  take it pre-dispatch and re-check state after acquiring; a hook that cannot
  acquire it during `preparing` aborts the git transaction (fail closed) after
  a bounded wait (`GIT_BARRIER_TIMEOUT_S`, default 5 s) and a clear operator
  message. The timeout makes a hung multiplex fail fast rather than block every
  commit indefinitely; the spooled/event probe still fences queries. Runbook:
  if commits fail with a barrier timeout, stop the multiplex; the OS releases
  the `flock` and commits proceed (the spool/probe repairs state).
- **Tombstones:** terminal transaction IDs are retained through the spool
  replay window, so a delayed `begin` cannot resurrect an active transaction.
- **Recovery:** an authenticated, audited `/admin/recover` clears a stranded
  transaction only after the lock/settle proof (or an explicit forced reason).
- **Spool bounds:** entry/byte quotas, quarantine for corrupt files, atomic
  deletion after apply.
- **Single writer:** one multiplex owner via an advisory lock; multi-instance
  operation is not supported in Phase 1.
- **Settle proof:** for ref-changing operations, require stable resolved HEAD,
  no relevant `index.lock`/`HEAD.lock`/checked-out-ref lock, and a short
  stability window before `ready`.
- **Scope limit:** file-level checkout (`git checkout -- <file>`) and
  uncommitted edits change no ref or index metadata; they are covered by
  Serena's file watching or an optional watcher, not by this epoch probe.

## 8. Implementation status

Landed:

- `src/engram/pipeline/serena_workspaces.py`: per-workspace epoch state, FIFO
  admission gate, worktree probe (deadline + process-group cleanup), typed
  busy exceptions, read-only allowlist, spool drain, shared/exclusive write
  barrier.
- `src/engram/pipeline/serena_supervisor.py`: lazy one-process-per-workspace
  supervisor with `{workspace}`/`{port}` command templating, readiness wait,
  and idle eviction.
- `serena_multiplex.py`: `/admin/invalidate` (token-gated), epoch-fenced
  scoped calls, typed responses, write/no-replay handling, `require-workspace`
  modes, `--workspace-upstream` map (distinct endpoints enforced),
  `--workspace-serena-command` supervisor, and `--workspace-barrier-dir`.
- `git-hooks/family-invalidate/`: invalidation templates + a fail-closed
  barrier broker (`serena_barrier.py`); `generate-hooks.sh` supports
  `HOOK_VARIANT=invalidate|restart`.
- Durable per-workspace tombstones and an authenticated, audited
  `/admin/recover` (refuses while a git lock is held unless `force` is set),
  so a delayed/replayed `begin` cannot resurrect a finished transaction.
  Tombstone state lives under `<spool>/.state/`, outside the drain glob;
  successful hook POSTs delete their spool copy, and duplicate terminal events
  are idempotent.
- Request-level liveness: per-workspace spawn/reuse performs a bounded MCP
  `initialize`; a hung-but-listening process is killed and respawned. The same
  bounded probe runs after `restart_language_server`.
- Authorization timeouts return typed `workspace_auth_timeout` (503, retry),
  distinct from a genuine 403 denial; exact allowlisted roots skip git.
- The worktree fingerprint excludes index mtime (IDE `git status` refreshes do
  not trigger restarts); locks and resolved HEAD remain the mutation signal.
- Tests: workspace state machine, tombstones/recovery, barrier broker,
  supervisor (including a hung listener), generator, admin endpoints, and
  auth-timeout typing (`tests/test_serena_workspaces.py`,
  `tests/test_serena_barrier.py`, `tests/test_serena_supervisor.py`,
  `tests/test_generate_family_hooks.py`).

Pending before the issue is closed:

- The settle proof is implemented (post-restart probe + lock check + optional
  stability window) but has not been exercised against real worktrees.
- The 7-day shadow migration in deployment.
- Live two-worktree verification and retirement of the deployed restart hook.

## 9. Evidence

- Same-project reactivation no longer restarts language servers: Serena
  `CHANGELOG`, "Reactivation of the same project restarted language servers
  (#1280)".
- LSP restart is optional: Serena docs, `restart_language_server`, and
  `RestartLanguageServerTool` in `src/serena/tools/symbol_tools.py`.
- One shared active project is a Serena design constraint: docs, "Multiple
  Agents Working on Different Projects — run a separate instance for each
  project".
- Epoch-guarded per-workspace snapshots and refresh admission: zvec-grep
  `rust/crates/zg-daemon/src/workspace_runtime.rs`.
