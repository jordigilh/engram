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
the one real permission gotcha found during the spike (a root-owned, `600`
credentials file was unreadable in-container until relaxed):

```bash
mkdir -p ~/.hindsight-linux/pg0-data
chown 1000:1000 ~/.hindsight-linux/pg0-data
chmod 644 ~/.config/gcloud/application_default_credentials.json
```

## 5. Install as a systemd service (Podman Quadlet)

```bash
mkdir -p ~/.config/containers/systemd
cp quadlet/hindsight.container ~/.config/containers/systemd/
# Uncomment `UserNS=keep-id` in the copied file -- required for rootless
# Podman (a regular, non-root user, which this guide assumes). Without it,
# rootless Podman's default UID mapping sends your host UID to CONTAINER
# uid 0, not container uid 1000 -- so the bind-mounted pg0 data directory
# (owned 1000:1000 per the chown above) shows up owned by uid 0 *inside*
# the container, and the non-root `hindsight` process silently can't write
# to it. Confirmed directly during the #9 spike: without this line the
# mount showed as "0 0" from inside the container; with it, "1000 1000"
# and retain/recall worked end to end. Leave it commented out only if
# you're instead deploying this as a root-owned system-level Quadlet
# (`/etc/containers/systemd/`) -- there it actively breaks startup rather
# than being a harmless no-op.
sed -i 's/^#UserNS=keep-id/UserNS=keep-id/' ~/.config/containers/systemd/hindsight.container
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
uv pip install --python ~/.hindsight/venv/bin/python -r requirements-dev.txt
uv pip install --python ~/.hindsight/venv/bin/python cocoindex

ln -sf "$(pwd)/nightly-learn.py" ~/.hindsight/nightly-learn.py
ln -sf "$(pwd)/flows/cocoindex-flows.py" ~/.hindsight/cocoindex-flows.py
ln -sf "$(pwd)/search/cocoindex-search.py" ~/.hindsight/cocoindex-search.py
ln -sf "$(pwd)/correction_gate.py" ~/.hindsight/correction_gate.py
ln -sf "$(pwd)/contradiction_resolution.py" ~/.hindsight/contradiction_resolution.py
ln -sf "$(pwd)/project_scope.py" ~/.hindsight/project_scope.py
ln -sf "$(pwd)/spike" ~/.hindsight/spike
```

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

### Connection timeouts / DNS failures only inside the container
Look for a leftover firewall rule from another container runtime (Docker,
CNI) on the host before assuming a Hindsight or Podman problem — see the
build-time note in step 4. `Network=host` in `quadlet/hindsight.container`
is the documented, validated fallback.

### Recall returns empty results
Same as macOS: the bank needs at least one retained item. Run
`~/.hindsight/venv/bin/python3 ~/.hindsight/nightly-learn.py` manually or
retain a test memory.
