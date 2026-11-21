# Serena per-workspace invalidation — rollout and verification

Companion to `docs/SERENA_PER_WORKSPACE_INVALIDATION.md` (issue #204).
Deployment-specific paths, tokens, plists, and service labels stay outside this
repository; this is the ordered procedure and the pass/fail checks.

## 1. Preconditions

- The deployed Serena build exposes `restart_language_server` in the context
  used by the multiplex. Startup fails closed if it does not.
- A dedicated invalidation token file exists with `0600` permissions.
- The spool directory and `<spool>/.state` are writable by both the git hooks
  and the multiplex user.
- You have at least two family worktrees (A = mutating, B = observer).

## 1.1 Canary note (2026-11-21)

A two-worktree canary on the kubernaut family showed that a **cold**
per-workspace Serena process does not answer the first `find_symbol` within
180 s (gopls still indexing), and one process held ~340 MB RSS before its gopls
child. Typed `workspace_flushing` responses behaved correctly, but the
process-per-workspace cold start is not interactive for dynamic review
worktrees.

Before enabling the supervisor broadly, choose one:

- keep the warm shared daemon and enable `restart_language_server` in
  `included_optional_tools` (process stays warm; restart is per active LSP);
- pre-warm and pin per-workspace processes for the small canonical repo set,
  with a low max-process cap; or
- accept first-use latency and keep the typed retry loop for worktrees.

The epoch fence and typed contracts are independent of this topology choice.

## 2. Deploy the multiplex (shadow)

Start the new multiplex with per-workspace endpoints and a readiness probe:

```text
--allowed-root <canonical-root>            # repeat per family root
--workspace-upstream <worktree>=<url>      # or --workspace-serena-command
--workspace-barrier-dir <spool>/.barrier
--invalidation-token-file <token-file>
--invalidation-spool-dir <spool>
--require-workspace warn
--lsp-readiness-symbol <stable symbol>     # or --lsp-readiness-file
```

`warn` logs `would_reject_unbound` for unbound clients and forwards them
legacy-style. Bound calls already use the epoch fence, so `warn` is **client
migration insurance, not a freshness requirement**.

Shadow is optional. Skip it (or use a short canary) when you have verified that
every Serena mount sends `?workspace=` and handles the typed errors. Going
straight to `enforce` only risks breaking unbound legacy clients; it does not
weaken the fence for bound workspaces. If you do run shadow, gate on zero new
`would_reject_unbound` entries in the client inventory.

## 3. Relink hooks

Generate with `HOOK_VARIANT=invalidate`:

```bash
./git-hooks/generate-hooks.sh <family>.vars <staging-dir>
```

Inspect the rendered scripts, then relink `post-checkout`, `post-merge`,
`post-rewrite`, and `reference-transaction` in each family worktree. Keep the
legacy restart scripts for one release. The old restart invocation must no
longer run on git operations.

## 4. Verification (four acceptance criteria)

1. **Cross-workspace continuity.** While committing repeatedly in worktree A,
   query symbols in B. Pass: every B response is 200; no ReadTimeout, no
   generic 503.
2. **Typed busy response.** Query A during its invalidation. Pass: either a
   fresh 200 or `503` with `X-Engram-Error: workspace_flushing`,
   `Retry-After`, and JSON-RPC `-32002`; a retry then succeeds.
3. **No silent staleness.** Change a symbol in A, commit, query A. Pass: only
   the new symbol is returned; the old symbol is gone.
4. **Hook retired.** Commit in any family worktree. Pass: the Serena launchd/
   systemd PID is unchanged across the burst; no forced restart log line.

Also check:
- `workspace_auth_timeout` (503, retry) instead of a 403 for a valid linked
  worktree under load.
- A hung-but-listening Serena is recycled after the liveness probe timeout.
- `/admin/recover` with a held git lock is refused unless `force` is set.

## 5. Enforce

Flip `--require-workspace enforce`. Verify criteria 1–4 again. Then remove the
legacy restart scripts from the deployment after one release.

## 6. Rollback

- Relink the previously generated restart-variant hooks.
- Restart the multiplex with `--upstream-mode=shared` and
  `--allow-shared-invalidation` only if emergency migration is required (this
  mode does not claim criterion 1).
- Drain or archive the spool and reset workspace state/epochs so stale
  `git_active` state does not leak into the legacy path.

## 7. Known residuals

- `reset --hard` / `cherry-pick` / `revert` can hold the exclusive barrier
  until `max-hold` (60 s); writes receive typed flushing and self-heal.
- A missing `serena_barrier.py` fails open; HEAD/fingerprint drift rechecks
  remain the fence.
- File-level `git checkout -- <file>` and uncommitted edits are outside the
  epoch probe; they rely on Serena's file watching or the optional watcher.
