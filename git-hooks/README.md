# Self-Healing Git Hooks — Templates and Examples

Plain POSIX-shell `post-checkout` / `post-merge` / `reference-transaction`
git hooks that keep two things from silently going stale as a repo's
working tree changes underneath a running Cursor (or any other MCP client)
session:

1. **Language-server staleness**: `gopls mcp` / `serena start-mcp-server`
   processes cache file state at startup. A `git checkout`/`pull`/`merge`/
   `reset`/`rebase` that rewrites files on disk without restarting these
   processes leaves them serving stale symbol/reference data.
2. **`.cursor/mcp.json` template drift** (family variant only): for a group
   of repos sharing one symlinked `.cursor/mcp.json` template, re-provisions
   that symlink on checkout.

See `docs/NEW_PROJECT_SETUP.md` step 14 for the full installation walk-through
and step 8a for the shared-daemon "family" concept these hooks assume.

## Which variant do I need?

| Your project is... | Use |
|---|---|
| A standalone repo with its own real (non-symlink) `.cursor/mcp.json`, no shared multi-repo daemon | [`generic/`](generic/) — ready to symlink as-is, no templating needed |
| One of several repos sharing one long-lived HTTP MCP daemon (e.g. one shared Serena instance for a repo family) | [`family/`](family/) — `.sh.tmpl` templates, rendered per-family by [`generate-hooks.sh`](generate-hooks.sh) |

Only the `family` variant needs a shared daemon restart at all. If you're
not sure, start with `generic/` — it's simpler and correct for the common
case (each repo runs its own gopls/serena, nothing shared).

## `generic/` — install directly, no generation step

```bash
d=/path/to/your-repo
ln -sf /path/to/engram/git-hooks/generic/post-checkout-generic-mcp.sh "$d/.git/hooks/post-checkout"
ln -sf /path/to/engram/git-hooks/generic/post-merge-generic-mcp.sh      "$d/.git/hooks/post-merge"
ln -sf /path/to/engram/git-hooks/generic/reference-transaction-generic-mcp.sh "$d/.git/hooks/reference-transaction"
```

`post-merge` and `reference-transaction` are also self-provisioned by
`post-checkout` on its own next run if either is missing, so re-linking just
`post-checkout` after a fresh clone is normally enough — link all three
explicitly for a brand-new onboarding rather than relying on that
self-healing to fire first.

Symlink (don't copy) so future fixes to the shared scripts land in every
repo without a re-run.

## `family/` — generate per-family, then install

1. Write a `.vars` config for your family (see
   [`families/kubernaut-family.vars`](families/kubernaut-family.vars) for a
   real, fully-worked example) with two required keys:
   - `FAMILY_NAME` — e.g. `"my-family"`; must match the launchd label suffix
     and `.cursor/mcp.json` template filename prefix you use elsewhere.
   - `DAEMON_LABELS` — space-separated full launchd labels to restart. **Only
     list a daemon that actually caches live checkout-file/symbol state in
     memory** (typically a Serena/gopls daemon with a shared "active
     project"). Do NOT list a DB/vector-backed code-search daemon (no live-file
     dependency) or a stateless HTTP relay (caches nothing of its own) — see
     `family/_restart-family-daemons.sh.tmpl`'s header for why, and
     `docs/findings/2026-08.md`'s 2026-08-16 entry for the real incident
     (a restart storm across 6+30 repos) this guidance comes from.
   - Optional: `DEBOUNCE_SECONDS` (default `30`) — collapses a burst of
     ref-changing git ops (a rebase, near-simultaneous hooks across repos,
     background mirror-fetch `reference-transaction`s) into one restart.

2. Generate:

   ```bash
   ./generate-hooks.sh families/my-family.vars ~/.hindsight/git-hooks
   ```

3. Symlink the generated scripts into each family repo the same way as the
   `generic/` variant above, using the generated filenames (e.g.
   `post-checkout-my-family-mcp.sh`).

## Why debounce, and why not just "restart if unresponsive"?

Every daemon these hooks manage should already have
`KeepAlive: SuccessfulExit=false` in its launchd/systemd unit — that already
gives you automatic restart-on-crash for free. These hooks are not crash
recovery; their only job is forcing a restart of an otherwise-*healthy*
process to flush stale in-memory file/symbol state after a checkout. A
liveness-only "restart if unresponsive" watchdog would duplicate
`KeepAlive` and still miss the actual failure mode (a live, responsive
daemon silently serving stale data) — see `docs/findings/2026-08.md`'s
2026-08-16 entry for the full investigation that led to debouncing instead.

## Known gaps (accepted, not bugs)

- Stock git has no dedicated post-rebase hook — `reference-transaction`
  (which fires on *any* ref-updating command, including `git reset`/
  `git rebase`) closes this gap for both variants.
- The family variant's single-shared-daemon model has an accepted
  known limitation if two agents on two different family repos both need
  "active project" state concurrently — see the family template header and
  `docs/NEW_PROJECT_SETUP.md` step 8a for the multiplex-daemon workaround
  (`engram-serena-multiplex`) if you need real per-repo isolation instead.
