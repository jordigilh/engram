# Native zvec-grep: the default-home contract

The native zvec-grep daemon (`io.vectorize.zvec-grep-native`) runs on zg's
**default home**, `$HOME/.zvec-grep`, and does not set `ZVEC_GREP_HOME`.
`zg` resolves daemon state from `ZVEC_GREP_HOME ?? ~/.zvec-grep`, so a plain
login shell, a deploy script and an agent session all reach the same daemon
without exporting anything. Issue
[#249](https://github.com/jordigilh/engram/issues/249) records why the previous
private home was a defect: a healthy daemon read as `Server: stopped` from every
ordinary shell.

## What lives where

| Path | Owner | Notes |
| --- | --- | --- |
| `$HOME/.zvec-grep/daemon/instance.lock` and `daemon/logs/` | zg | The deploy tool is read-only here; `lock-repair` is the only exception and it never deletes. |
| `$HOME/.zvec-grep/workspaces.json` | user | The workspace registry. Never rewritten or migrated by deploy tooling. |
| `<repo>/.zvec-grep/` | user | Per-workspace indexes. Unaffected by a home change. |
| `$HOME/.local/zvec-grep-native/bin/zg` | native install | The daemon binary. |
| `$HOME/Library/LaunchAgents/io.vectorize.zvec-grep-native.plist` | deploy tool | Rendered from `launchd/io.vectorize.zvec-grep-native.plist`. |
| `$HOME/.zvec-grep/deploy/manifest.json` | deploy tool | Hashes the reviewed template, the deployed render and the bootstrapped render. |
| `$HOME/.zvec-grep/deploy/backups/` | deploy tool | Timestamped plist backups, last five kept, mode 0600. |
| `$HOME/.engram/zvec-grep/models` | user | Model cache; deliberately outside the daemon home and still set explicitly. |

`ZVEC_GREP_MODEL_CACHE` and `ZVEC_GREP_MCP_SESSION_IDLE_SECONDS` stay in the
template because they are deployment settings, not daemon-state location.
`ZVEC_GREP_WORKSPACE_ROOTS` is intentionally absent: the native daemon already
has the user's filesystem authority, and that allowlist exists to constrain
container bind mounts.

## Install, verify, rollback

```bash
# First install on a machine with no default home yet (creates it 0700).
engram-zvec-grep-native install --allow-empty-home

# Normal re-run: idempotent; writes nothing when the render already matches.
engram-zvec-grep-native install

# Replace or converge a loaded job (an explicit change window).
engram-zvec-grep-native install --restart

# Prove the deployed job uses the default home.
engram-zvec-grep-native verify

# Remove only the plist this tool installs. State, indexes and workspaces stay.
engram-zvec-grep-native uninstall
```

`install` fails closed and writes nothing when: the platform is not macOS; the
caller exports a `ZVEC_GREP_HOME` that points elsewhere; `HOME` is relative or a
symlinked default home; the native binary is missing; the container job
`io.vectorize.zvec-grep` is loaded (one daemon per `127.0.0.1:7999`); the home is
not owned by the user or is group/world writable; `workspaces.json` is missing or
empty (unless `--allow-empty-home`); the state is foreign or cannot be verified;
a changed render would land behind a running job without `--restart`; the loaded
job is older than the deployed plist without `--restart`; or the listen port is
answered by a listener that is not this job. `--template <path>` points at a
specific template when running from a non-editable install (the default resolves
the template from the checkout). After a successful bootstrap, `install` waits
(bounded) for the daemon to publish a ready default-home instance lock before it
verifies, because `launchctl bootstrap` returns before zg opens its listener.

`verify` requires the deployed plist to match the manifest **and** the current
reviewed template, the bootstrapped hash to match the deployed file (catching a
job that is still running an older definition), the loaded pid to equal the
default-home lock pid, and a clean-environment
`zg --server status --check-ready` against the absolute deployed binary to print
`ready`. If a `zg` on `PATH` resolves to a different binary it fails and names
it: a leftover shim that overrides `ZVEC_GREP_HOME` is exactly the #249 defect
in a different place. A missing `zg` on `PATH` is only a warning.

Rollback: `install` prints the exact backup it wrote under
`$HOME/.zvec-grep/deploy/backups/` when a restart fails, and restores that plist
automatically. To roll back a successful change deliberately, `uninstall`, copy
the chosen backup over `~/Library/LaunchAgents/io.vectorize.zvec-grep-native.plist`,
and re-adopt it with `engram-zvec-grep-native install --restart --template
<previous-template>`, or bootstrap it directly:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.vectorize.zvec-grep-native.plist
```

`uninstall` also records `uninstalled_at` in the manifest, so a later install
treats the deployment as new. A failed bootstrap restores the previous plist and
records the restored render hash, so the deployment stays verifiable and
uninstallable instead of being mistaken for unmanaged.

## Cutover from the private home (one-time, explicit)

A host that already ran the private-home job keeps its legacy state at
`$HOME/.local/zvec-grep-native/home`. The registry and indexes are already in
their final places, so the cutover changes only which home the daemon writes to.
Do it when no long index rebuild is running:

1. Quiesce the daemon and stop the job:
   `launchctl bootout gui/$(id -u)/io.vectorize.zvec-grep-native`.
   A stale legacy lock is expected and is read-only for this tooling.
2. Remove any temporary `/Users/jgil/bin/zg`-style wrapper that exports the
   private home. Leaving it re-creates #249 for every shell.
3. `engram-zvec-grep-native install --restart` (backs up the old plist first).
4. `engram-zvec-grep-native verify`; then from a clean environment confirm the
   CLI agrees:
   ```bash
   env -i HOME="$HOME" PATH="$HOME/.local/zvec-grep-native/bin:/usr/bin:/bin" \
     zg --server status --check-ready
   ```
5. If the cutover must be undone, `uninstall`, restore the backup printed by
   install, and re-adopt it as described under "Install, verify, rollback"; the
   temporary wrapper is only re-created when deliberately reverting to the
   private home.

## The one supported exception

A deliberately isolated second daemon may keep a private home, but only with
**both** an explicit `ZVEC_GREP_HOME` and a distinct `--listen` port. Two jobs on
`127.0.0.1:7999` are refused by design. `engram-zvec-grep-native` only targets
the default home, so the exception is managed outside this tool and cannot be
half-configured by it.

## Stale locks

`zg` owns lock recovery. If a lock remains with a dead pid and the job cannot
start, use the explicit repair after stopping the job:

```bash
launchctl bootout gui/$(id -u)/io.vectorize.zvec-grep-native
engram-zvec-grep-native lock-repair --confirm-dead-lock
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.vectorize.zvec-grep-native.plist
```

`lock-repair` refuses a loaded job, refuses a live pid, refuses the legacy home,
accepts a malformed lock under `--confirm-dead-lock`, and renames the lock to a
unique `instance.lock.stale-<UTC>` (never deletes, so prior evidence survives
repeated repairs).
