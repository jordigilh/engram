# Installation Guide — Linux / Fedora / RHEL

This is the Linux counterpart to [`INSTALL.md`](INSTALL.md). It only covers
the steps that differ by platform: prerequisites, running the Hindsight
service itself, and scheduling the batch/ingestion scripts. Everything else
in `INSTALL.md` — Cursor MCP config, the Cursor rule, CocoIndex setup,
mental models, docs/issues ingestion (steps 1–3 and 7–18) — is
platform-agnostic Python/Cursor tooling and applies unchanged on Linux.
Follow this doc for the platform-specific pieces, then jump to `INSTALL.md`
step 7 onward for the rest.

**Architecture**: unlike macOS (native process, no container), Hindsight
itself runs **containerized** on Linux via Podman, while the batch scripts
(`nightly-learn.py`, `cocoindex-flows.py`, `cocoindex-search.py`) run
**natively** via the same hermetic `uv`-managed Python venv the macOS install
uses — see [FINDINGS.md](FINDINGS.md) 2026-07-29 ("Decided architecture") for
why the split isn't symmetric, and 2026-07-29 ("#9 Implemented") for what was
actually validated (build, boot, full retain/recall round-trip, real data
persistence across container restarts, and this doc's Quadlet + systemd units
all deployed and tested end-to-end via real systemd on RHEL 9).

## Prerequisites

- Fedora or RHEL 9+ (tested on RHEL 9.0). Any recent systemd-based distro with
  Podman should work; Fedora/RHEL are the primary targets because Podman ships
  by default and this project's prior container deployment already used it.
- Podman 4.x+ with **Quadlet** support (`/usr/libexec/podman/quadlet -version`
  should print a version; if missing, `sudo dnf install podman`).
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`.
  Exactly like the macOS install, this gives the batch scripts a
  version-pinned Python (3.14) independent of whatever the distro ships
  (RHEL 9's default `python3` is 3.9 — too old for this project's syntax —
  but that's irrelevant once `uv venv --python 3.14` is in the picture).
- [gh](https://cli.github.com/) and [jq](https://jqlang.github.io/jq/) —
  `sudo dnf install gh jq` (or see each tool's Linux install docs).
- Google Cloud SDK (`gcloud`) with Application Default Credentials configured.
- Vertex AI API enabled, Claude models enabled — same as `INSTALL.md`.
- **SELinux**: if enforcing (the Fedora/RHEL default), every bind-mounted
  volume in the Quadlet unit below already carries the `:Z` label so Podman
  relabels it automatically — no separate `setsebool`/`semanage` step needed
  for this project's mounts specifically. If you hit `Permission denied`
  errors that mention SELinux/AVC in `journalctl`, check `getenforce` first.

## 1–3. Clone, authenticate, configure

Identical to `INSTALL.md` steps 1–3 — `~/.hindsight/config.env` is the same
single source of truth on every platform.

## 4. Build and run Hindsight (containerized)

```bash
cd engram
podman build -t localhost/engram-hindsight:latest -f Dockerfile .
```

> **If the build hangs or fails resolving `pypi.org`** on a shared host that
> also runs Docker or another container runtime: this hit us during the #9
> spike (see FINDINGS.md) and was a stale Docker/CNI-installed firewall rule
> conflicting with Podman's own bridge network, not a Fedora/RHEL/Podman
> problem. Confirm with `podman build --network=host -t localhost/engram-hindsight:latest -f Dockerfile .` —
> if that succeeds where the default bridge build fails, look for leftover
> `nft`/`firewalld` rules from another runtime rather than assuming Hindsight
> or Podman is broken.

Set up the persistent data directory and credentials. The image runs as a
**non-root `hindsight` user (uid 1000)** internally, so a bind-mounted host
directory needs matching ownership, not just SELinux relabeling — this is
the one real permission gotcha found during the original spike (a
root-owned, `600` credentials file was unreadable in-container until
relaxed):

```bash
mkdir -p ~/.hindsight-linux/pg0-data
chown "$(id -u):$(id -g)" ~/.hindsight-linux/pg0-data
chmod 644 ~/.config/gcloud/application_default_credentials.json
```

> **Use `$(id -u):$(id -g)`, not a hardcoded `1000:1000`** (fixed
> 2026-08-13 after a real second-host spike hit this): the ownership above
> only needs to match container UID 1000 when combined with the
> `keep-id:uid=1000,gid=1000` form in step 5 below — see that step for why
> a hardcoded `1000:1000` silently breaks on any host where your deploying
> user's own UID isn't 1000 (common on shared/multi-user hosts, or simply
> not the first account created).

## 5. Install as a systemd service (Podman Quadlet)

```bash
mkdir -p ~/.config/containers/systemd
cp quadlet/hindsight.container ~/.config/containers/systemd/
# Uncomment `UserNS=keep-id:uid=1000,gid=1000` in the copied file --
# required for rootless Podman (a regular, non-root user, which this guide
# assumes). Without SOME form of --userns, rootless Podman's default UID
# mapping sends your host UID to CONTAINER uid 0, not container uid 1000 --
# so the bind-mounted pg0 data directory shows up owned by uid 0 *inside*
# the container, and the non-root `hindsight` process silently can't write
# to it.
#
# IMPORTANT -- use `keep-id:uid=1000,gid=1000`, not bare `keep-id` (fixed
# 2026-08-13, second real-host spike on a shared RHEL 9 lab VM where the
# deploying user's own UID was 1005, not 1000): bare `keep-id` identity-maps
# your OWN host UID to itself inside the container (e.g. 1005 -> 1005) --
# it does NOT map you to container UID 1000, which is where the `hindsight`
# process actually runs. The original 2026-07-29 spike's `chown 1000:1000` +
# bare `keep-id` combination only worked because that spike's deploying user
# happened to itself be UID 1000 (a common but not universal "first regular
# user" default) -- confirmed reproducible on a second host where the user
# was a different UID: bare `keep-id` fails outright with the image's own
# startup check ("embedded database directory ... is not writable by this
# container (UID 1000)"), regardless of what the host directory is chowned
# to, since the container never sees your UID as 1000 in the first place.
# `keep-id:uid=1000,gid=1000` instead maps your OWN host UID to container
# UID 1000 specifically (whatever your real host UID actually is), which is
# what makes the plain `chown $(id -u):$(id -g)` in step 4 correct
# regardless of your deploying user's actual UID. Leave both keep-id forms
# commented out only if you're instead deploying this as a root-owned
# system-level Quadlet (`/etc/containers/systemd/`) -- there it actively
# breaks startup rather than being a harmless no-op.
sed -i 's/^#UserNS=keep-id:uid=1000,gid=1000/UserNS=keep-id:uid=1000,gid=1000/' ~/.config/containers/systemd/hindsight.container
loginctl enable-linger "$USER"   # user-level units otherwise stop at logout
systemctl --user daemon-reload
systemctl --user enable --now hindsight.service
```

`hindsight.container` reads `~/.hindsight/config.env` for all LLM/project
config (same as `with-config-env.sh` does for the macOS native install) —
nothing project-specific is baked into the unit file itself. See the
comments at the top of [`quadlet/hindsight.container`](../quadlet/hindsight.container)
for the full rationale, including the validated `Network=host` fallback if
you hit connection timeouts under standard bridge networking (same
underlying cause as the build-time DNS issue above, on hosts affected by it).

Check status and logs the systemd way, not `podman logs` directly (though
that still works too):

```bash
systemctl --user status hindsight.service
journalctl --user -u hindsight.service -f
```

## 6. Verify

Identical checks to `INSTALL.md` step 6:

```bash
curl -s http://localhost:8888/health | python3 -m json.tool
# Expected: {"status": "healthy", "database": "connected"}
```

Data persistence was explicitly validated during the #9 spike: a bank
created before a `systemctl --user restart hindsight.service` (which
destroys and recreates the container, per Quadlet's `--rm` semantics) was
still present after — confirmed both via the app's own "Existing pg0 data
directory detected" startup log line and by querying the bank after restart.

## 7. Install the batch scripts (native, same as macOS)

```bash
uv venv ~/.hindsight/venv --python 3.14
uv pip install --python ~/.hindsight/venv/bin/python \
  'hindsight-api[all]' 'google-cloud-aiplatform>=1.38'
uv pip install --python ~/.hindsight/venv/bin/python -e ".[dev]"
uv pip install --python ~/.hindsight/venv/bin/python cocoindex

ln -sf "$(pwd)/src/engram/pipeline/nightly_learn.py" ~/.hindsight/nightly-learn.py
ln -sf "$(pwd)/src/engram/pipeline/ingest_issues.py" ~/.hindsight/ingest-issues.py
ln -sf "$(pwd)/src/engram/flows/kubernaut.py" ~/.hindsight/cocoindex-flows.py
ln -sf "$(pwd)/src/engram/search/kubernaut.py" ~/.hindsight/cocoindex-search.py
```

`uv pip install -e ".[dev]"` is the one-shot editable install of the whole
`engram` package (see [`INSTALL.md`](INSTALL.md) step 9) — it makes
`correction_gate.py`, `contradiction_resolution.py`, `project_scope.py` etc.
importable as `engram.*` and generates the `engram-flows-kubernaut` /
`engram-search-kubernaut` / `engram-nightly-learn` / `engram-ingest-issues`
console scripts in `~/.hindsight/venv/bin/`, so no per-shared-module symlinks
or `spike/` path hack are needed here either. The two remaining symlinks
above are only for the systemd-invoked entry points below (a planned
follow-up will point those units at the console scripts directly instead
and retire the symlinks — not yet done).

These run **natively**, not containerized — the host-Python-version concern
that would justify containerizing them doesn't apply once `uv venv --python
3.14` is in the picture (identical reasoning to why the macOS install doesn't
containerize them either; see the top of this doc).

## 8. Schedule with systemd timers

The Linux equivalents of `launchd/io.vectorize.hindsight.{hourly,nightly}.plist`
and `io.vectorize.cocoindex.service.plist`:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/engram-hindsight-hourly.{service,timer} ~/.config/systemd/user/
cp systemd/engram-hindsight-nightly.{service,timer} ~/.config/systemd/user/
cp systemd/engram-cocoindex.service ~/.config/systemd/user/
systemctl --user daemon-reload

systemctl --user enable --now engram-hindsight-hourly.timer
systemctl --user enable --now engram-hindsight-nightly.timer
systemctl --user enable --now engram-cocoindex.service
```

Verify the timers are scheduled and the CocoIndex service is live:

```bash
systemctl --user list-timers 'engram-*'
journalctl --user -u engram-cocoindex.service -f
```

> **Add `ENGRAM_DOCS_DIR`, `ENGRAM_CODE_DIR`, etc. to `~/.hindsight/config.env`**
> before starting `engram-cocoindex.service` — same as `INSTALL.md` step 16.
> The systemd unit reads them from there, not from the unit file itself.

> **Lingering sessions**: user-level systemd units (`systemctl --user`) only
> keep running while a login session exists unless linger is enabled. Run
> `loginctl enable-linger $USER` once so the timers/service survive logout —
> the direct equivalent of launchd's LaunchAgents starting at login without
> needing an active terminal session.

## 9. (Optional, Repo Families Only) Shared cocoindex-code/Serena Daemons + Serena Multiplex

If you're onboarding a **family of repos under one org that should share one
code-intelligence backend** instead of spawning a `cocoindex-code`/`serena`/
`gopls` subprocess per Cursor window per repo — see
[`NEW_PROJECT_SETUP.md`](NEW_PROJECT_SETUP.md)'s step 8 "Evolution" note and
step 8a for the full, platform-agnostic rationale (when this is worth the
added complexity, the "one active project at a time" limitation of a plain
shared Serena daemon, and why the multiplex wrapper exists). This section is
just the Linux (`systemd --user`) command sequence; the concepts, `.cursor/
mcp.json` shapes, and gotchas are identical to the macOS (`launchd`)
instructions there.

```bash
cp systemd/engram-cocoindex-code-kubernaut-family.service ~/.config/systemd/user/
cp systemd/engram-serena-kubernaut-family.service ~/.config/systemd/user/
cp systemd/engram-serena-project-server.service ~/.config/systemd/user/
cp systemd/engram-serena-multiplex-kubernaut-family.service ~/.config/systemd/user/
systemctl --user daemon-reload

systemctl --user enable --now engram-cocoindex-code-kubernaut-family.service
systemctl --user enable --now engram-serena-kubernaut-family.service
systemctl --user enable --now engram-serena-project-server.service
systemctl --user enable --now engram-serena-multiplex-kubernaut-family.service
```

The 4 unit files are named/scoped for this project's own repo family
(`kubernaut`, `kubernaut-operator`, `kubernaut-console`,
`kubernaut-demo-scenarios`, `kubernaut-v1.5`, `kubernaut-v1.6`) — for a
different family, copy and rename them, and edit each unit's `--project`/
`ExecStart` args plus `.cursor/mcp.json`'s ports/URLs to match. See each
unit file's own header comments (and the direct-analog `launchd/*.plist`
they mirror) for the full per-flag rationale.

> **Postgres reachability gotcha specific to Linux (confirmed via a live
> spike, 2026-08-13)**: the `cocoindex-code` daemon's `COCOINDEX_PG_URL`
> needs `localhost:5432` reachable from a **native** host process. On
> macOS this works because Postgres either runs natively or its embedded
> `pg0` is otherwise reachable on the host loopback; on Linux, step 5's
> Podman Quadlet only publishes port 8888 by default, **not** 5432 — live
> reproduced on a real RHEL 9 host: with only `PublishPort=8888:8888`, a
> raw TCP connection attempt to `localhost:5432` got an immediate
> connection-refused (not a hang), confirming it's genuinely unreachable,
> not just slow. Fix, also confirmed live: adding `PublishPort=5432:5432`
> to `~/.config/containers/systemd/hindsight.container` and restarting the
> service made 5432 immediately reachable, and the `cocoindex-code` daemon
> started and served `tools/list` correctly against it with no further
> changes. (Running under that Quadlet's `Network=host` fallback instead
> would also make 5432 reachable with no `PublishPort` needed, but this
> wasn't separately re-tested this session — bridge + explicit
> `PublishPort` was.) Verify with
> `psql -h localhost -p 5432 -U hindsight -d hindsight -c 'select 1;'`
> (or a plain `bash -c 'echo > /dev/tcp/localhost/5432'` reachability check
> if `psql` isn't installed) before enabling the unit.

Give each family repo its own `.cursor/mcp.json` `serena` entry pointed at
its own multiplex mount, exactly as `NEW_PROJECT_SETUP.md` step 8a
describes — this part is pure JSON config and identical on every platform:

```json
"serena": { "type": "http", "url": "http://127.0.0.1:8893/mcp/<project-name>" }
```

Verify the daemons are up and correctly isolating per project:

```bash
systemctl --user status engram-serena-multiplex-kubernaut-family.service
journalctl --user -u engram-serena-multiplex-kubernaut-family.service -f
```

> **Personal git-hook restart scripts are not part of this repo**: on
> macOS, `~/.hindsight/git-hooks/_restart-kubernaut-family-daemons.sh`
> (a personal, uncommitted script — see `NEW_PROJECT_SETUP.md` step 14) uses
> `launchctl kickstart -k` to restart these 4 daemons after a `git checkout`/
> `pull`/`rebase` changes files on disk underneath them. If you build the
> Linux equivalent of that self-healing git-hook family for your own repos,
> swap the restart calls for `systemctl --user restart
> <unit-name>.service` — nothing else about the hook logic (detecting HEAD
> changes, self-provisioning `post-merge`/`reference-transaction`) is
> platform-specific.

## Continue with the platform-agnostic steps

Jump to [`INSTALL.md`](INSTALL.md) step 7 ("Configure Cursor MCP") onward —
Cursor integration, the hindsight-memory rule, docs/issues ingestion, mental
models, and the test suite are identical on every platform.

## Troubleshooting

### Service won't start
```bash
systemctl --user status hindsight.service
journalctl --user -u hindsight.service --no-pager | tail -50
```

### `Permission denied` reading credentials inside the container
The container runs as non-root uid 1000. Check the host-side file is
world-readable (`chmod 644`), not just correctly SELinux-labeled — `:Z` alone
does not fix a `600`/root-owned file.

### `The embedded database directory /home/hindsight/.pg0 is not writable by this container (UID 1000)`
You're using bare `UserNS=keep-id` instead of `UserNS=keep-id:uid=1000,gid=1000`
(step 5), and your deploying user's own UID isn't 1000. Bare `keep-id`
identity-maps your own UID to itself inside the container (e.g. host UID
1005 stays 1005 inside), which does nothing for the `hindsight` process,
still fixed at container UID 1000 — no `chown` value on the host directory
can fix this, since the container never sees your UID as 1000 in the first
place. Fix: use `UserNS=keep-id:uid=1000,gid=1000` in the Quadlet, and
`chown "$(id -u):$(id -g)"` (not a hardcoded `1000:1000`) on
`~/.hindsight-linux/pg0-data`. Confirmed live 2026-08-13 on a host where
the deploying user was UID 1005 — this exact error, this exact fix.

### Connection timeouts / DNS failures only inside the container
Look for a leftover firewall rule from another container runtime (Docker,
CNI) on the host before assuming a Hindsight or Podman problem — see the
build-time note in step 4. `Network=host` in `quadlet/hindsight.container`
is the documented, validated fallback.

### Recall returns empty results
Same as macOS: the bank needs at least one retained item. Run
`~/.hindsight/venv/bin/python3 ~/.hindsight/nightly-learn.py` manually or
retain a test memory.
