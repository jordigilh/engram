"""Deploy and verify the native zvec-grep daemon on zg's default home.

Issue #249: the native launchd job used to run under a private
``ZVEC_GREP_HOME`` that no CLI caller inherited, so a healthy daemon read as
"stopped" from every ordinary shell. This module owns the durable deployment
path: a launchd template that deliberately omits ``ZVEC_GREP_HOME``, an
idempotent installer, a fail-closed verifier, an uninstall path, and the one
sanctioned repair for a stale default-home lock.

State ownership
---------------
* zg owns ``<home>/daemon/instance.lock`` and its logs. This tool never
  mutates them; ``lock-repair`` is the only exception and it never deletes.
* This tool owns the deployed plist, the manifest under
  ``<home>/deploy/manifest.json`` and timestamped backups under
  ``<home>/deploy/backups``.
* The user owns ``workspaces.json``, workspace indexes and the model cache.

The default home is ``$HOME/.zvec-grep``. A deliberately isolated second
daemon is the only supported exception and must set ``ZVEC_GREP_HOME``
explicitly *and* a distinct listen port; this tooling only targets the
default home.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

LABEL = "io.vectorize.zvec-grep-native"
CONTAINER_LABEL = "io.vectorize.zvec-grep"
DEFAULT_LISTEN = "127.0.0.1:7999"
BACKUP_LIMIT = 5
BOOTSTRAP_ATTEMPTS = 3
BOOTSTRAP_BACKOFF_SECONDS = (1.0, 2.0)

STATE_OURS = "ours"
STATE_OURS_LEGACY = "ours-legacy"
STATE_FOREIGN = "foreign"
STATE_LOADED_NO_PID = "loaded-no-pid"
STATE_LOADED_STALE_LOCK = "loaded-stale-lock"
STATE_STALE = "stale"
STATE_ABSENT = "absent"
STATE_UNVERIFIED = "unverified"

_XML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_PID_PATTERN = re.compile(r'^\s*"?PID"?\s*=\s*(\d+)\s*;', re.MULTILINE)


class DeploymentError(RuntimeError):
    """A fail-closed deployment condition with an operator-facing message."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class Deps:
    """Injected environment so the unit tier never touches launchd or a home."""

    run: Callable[[Sequence[str]], CommandResult] = field(
        default=lambda argv: _default_run(argv)
    )
    exec_clean: Callable[[Sequence[str], dict[str, str]], CommandResult] = field(
        default=lambda argv, env: _default_exec(argv, env)
    )
    pid_alive: Callable[[int], bool] = field(default=lambda pid: _default_pid_alive(pid))
    port_open: Callable[[str, int], bool] = field(default=lambda host, port: _default_port_open(host, port))
    which: Callable[[str], str | None] = field(default=lambda name: shutil.which(name))
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    sleep: Callable[[float], None] = field(default=time.sleep)
    uid: int = field(default_factory=os.getuid)
    platform: str = field(default_factory=lambda: sys.platform)


@dataclass(frozen=True)
class DeployPaths:
    home: Path
    native_root: Path
    default_home: Path
    legacy_home: Path
    model_cache: Path
    plist: Path
    manifest: Path
    backups: Path
    template: Path

    @property
    def default_lock(self) -> Path:
        return self.default_home / "daemon" / "instance.lock"

    @property
    def legacy_lock(self) -> Path:
        return self.legacy_home / "daemon" / "instance.lock"

    @property
    def binary(self) -> Path:
        return self.native_root / "bin" / "zg"


def resolve_paths(home: Path, template: Path | None = None) -> DeployPaths:
    home = home.expanduser()
    native_root = home / ".local" / "zvec-grep-native"
    default_home = home / ".zvec-grep"
    template = template or Path(__file__).resolve().parents[3] / "launchd" / f"{LABEL}.plist"
    return DeployPaths(
        home=home,
        native_root=native_root,
        default_home=default_home,
        legacy_home=native_root / "home",
        model_cache=home / ".engram" / "zvec-grep" / "models",
        plist=home / "Library" / "LaunchAgents" / f"{LABEL}.plist",
        manifest=default_home / "deploy" / "manifest.json",
        backups=default_home / "deploy" / "backups",
        template=Path(template),
    )


# ---------------------------------------------------------------------------
# Rendering and integrity


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_home_text(home_text: str) -> None:
    if not home_text.startswith("/"):
        raise DeploymentError(f"HOME must be an absolute path (got {home_text!r})")
    if not home_text or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in home_text):
        raise DeploymentError("HOME contains control characters")
    if "__HOME__" in home_text:
        raise DeploymentError("HOME must not contain the template placeholder __HOME__")


def _xml_escape(text: str) -> str:
    for character, entity in _XML_ESCAPES.items():
        text = text.replace(character, entity)
    return text


def render_plist(template_text: str, paths: DeployPaths) -> bytes:
    """Render the reviewed template by substitution, then validate the result.

    Substitution preserves comments and ordering, so the deployed file stays
    reviewable and diffable against the template. ``plistlib`` is used only to
    validate the rendered XML and its contract, never to re-serialise it.
    """
    _validate_home_text(str(paths.home))
    rendered = template_text.replace("__HOME__", _xml_escape(str(paths.home)))
    try:
        parsed = plistlib.loads(rendered.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a deployment error
        raise DeploymentError(f"rendered plist is not valid: {exc}") from exc
    _validate_contract(parsed, paths)
    return rendered.encode("utf-8")


def _validate_contract(parsed: dict, paths: DeployPaths) -> None:
    if parsed.get("Label") != LABEL:
        raise DeploymentError(f"rendered plist label is {parsed.get('Label')!r}, expected {LABEL!r}")
    arguments = parsed.get("ProgramArguments") or []
    if not arguments or arguments[0] != str(paths.binary):
        raise DeploymentError("rendered plist must exec the native zg binary as its first argument")
    if "--listen" not in arguments or arguments.index("--listen") + 1 >= len(arguments):
        raise DeploymentError(f"rendered plist must listen on {DEFAULT_LISTEN}")
    if arguments[arguments.index("--listen") + 1] != DEFAULT_LISTEN:
        raise DeploymentError(f"rendered plist must listen on {DEFAULT_LISTEN}")
    environment = parsed.get("EnvironmentVariables") or {}
    if "ZVEC_GREP_HOME" in environment:
        raise DeploymentError("rendered plist must not set ZVEC_GREP_HOME: callers must inherit the default home")
    if environment.get("HOME") != str(paths.home):
        raise DeploymentError("rendered plist must pin HOME to the deploying user's home")
    if environment.get("ZVEC_GREP_MODEL_CACHE") != str(paths.model_cache):
        raise DeploymentError("rendered plist must keep the model cache outside the daemon home")


def _read_plist_bytes(path: Path) -> bytes | None:
    if not os.path.lexists(path):
        return None
    if path.is_symlink():
        raise DeploymentError(f"refusing to use a symlinked plist: {path}")
    if not path.is_file():
        raise DeploymentError(f"plist path is not a regular file: {path}")
    return path.read_bytes()


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise DeploymentError(f"refusing to write through a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _free_path(path: Path) -> Path:
    if not os.path.lexists(path):
        return path
    suffix = 1
    while True:
        suffix += 1
        candidate = path.with_name(f"{path.name}-{suffix}")
        if not os.path.lexists(candidate):
            return candidate


# ---------------------------------------------------------------------------
# Manifest and backups


@dataclass(frozen=True)
class Manifest:
    label: str = LABEL
    template_sha256: str = ""
    render_sha256: str = ""
    rendered_at: str = ""
    bootstrapped_render_sha: str | None = None
    created_home: bool = False
    uninstalled_at: str | None = None

    def to_bytes(self) -> bytes:
        return (json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Manifest":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeploymentError(f"deployment manifest is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise DeploymentError("deployment manifest must be a JSON object")
        known = {name: payload.get(name) for name in cls.__dataclass_fields__}
        return cls(**known)


def load_manifest(path: Path) -> Manifest | None:
    if not os.path.lexists(path):
        return None
    if path.is_symlink():
        raise DeploymentError(f"refusing to read a symlinked manifest: {path}")
    return Manifest.from_bytes(path.read_bytes())


def _write_manifest(paths: DeployPaths, manifest: Manifest) -> None:
    _atomic_write(paths.manifest, manifest.to_bytes(), 0o600)


def _is_ours(manifest: Manifest | None, deployed_hash: str | None) -> bool:
    if manifest is None or manifest.uninstalled_at is not None or deployed_hash is None:
        return False
    return manifest.render_sha256 == deployed_hash


def _backup_plist(paths: DeployPaths, payload: bytes, deps: Deps) -> Path | None:
    if not os.path.lexists(paths.plist):
        return None
    paths.backups.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.backups, 0o700)
    target = _free_path(paths.backups / f"{LABEL}.plist.{_utc_stamp(deps.now())}")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    _prune_backups(paths, deps)
    return target


def _prune_backups(paths: DeployPaths, deps: Deps) -> None:
    if not paths.backups.is_dir():
        return
    stamped: list[tuple[datetime, Path]] = []
    for entry in paths.backups.iterdir():
        prefix = f"{LABEL}.plist."
        if not entry.name.startswith(prefix):
            continue
        stamp_text = entry.name[len(prefix):].split("-", 1)[0]
        try:
            moment = datetime.strptime(stamp_text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        stamped.append((moment, entry))
    stamped.sort(key=lambda item: item[0])
    for _moment, entry in stamped[: max(0, len(stamped) - BACKUP_LIMIT)]:
        if entry.is_symlink() or entry.is_file():
            entry.unlink()


# ---------------------------------------------------------------------------
# launchd and daemon observation


@dataclass(frozen=True)
class JobProbe:
    loaded: bool | None
    pid: int | None = None


@dataclass(frozen=True)
class LockRecord:
    pid: int
    listen: str | None = None
    server_url: str | None = None
    ready: bool | None = None


@dataclass(frozen=True)
class Classification:
    kind: str
    job: JobProbe
    default_lock: LockRecord | None
    legacy_lock: LockRecord | None
    default_live: bool
    legacy_live: bool


def _default_run(argv: Sequence[str]) -> CommandResult:  # pragma: no cover - host dependent
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host dependent
        return CommandResult(127, "", str(exc))
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _default_exec(argv: Sequence[str], env: dict[str, str]) -> CommandResult:  # pragma: no cover - host dependent
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host dependent
        return CommandResult(127, "", str(exc))
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _default_pid_alive(pid: int) -> bool:  # pragma: no cover - host dependent
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _default_port_open(host: str, port: int) -> bool:  # pragma: no cover - host dependent
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _parse_table(text: str) -> JobProbe | None:
    """Parse the no-argument ``launchctl list`` table (PID status label)."""
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        pid_text, _status, label = fields[0], fields[1], fields[2]
        if label != LABEL:
            continue
        if pid_text == "-":
            return JobProbe(loaded=True, pid=None)
        if pid_text.isdigit():
            return JobProbe(loaded=True, pid=int(pid_text))
        return None
    return JobProbe(loaded=False, pid=None)


def _parse_direct(text: str) -> JobProbe:
    match = _PID_PATTERN.search(text)
    return JobProbe(loaded=True, pid=int(match.group(1)) if match else None)


def query_job(deps: Deps) -> JobProbe:
    """Read the loaded job's pid using two independent primitives.

    Neither primitive is trusted alone: the label-specific dictionary wins when
    available, the table is the fallback, and disagreement or unparseable
    output is reported as unverified instead of guessed.
    """
    direct = deps.run(["launchctl", "list", LABEL])
    table = deps.run(["launchctl", "list"])
    direct_probe = _parse_direct(direct.stdout) if direct.returncode == 0 else None
    table_probe = _parse_table(table.stdout) if table.returncode == 0 else None
    if direct_probe is None and table_probe is None:
        return JobProbe(loaded=None)
    if direct_probe is not None and table_probe is not None:
        if direct_probe.loaded and table_probe.loaded:
            if direct_probe.pid is not None and table_probe.pid is not None and direct_probe.pid != table_probe.pid:
                return JobProbe(loaded=None)
            return direct_probe if direct_probe.pid is not None else table_probe
        if direct_probe.loaded != table_probe.loaded:
            return JobProbe(loaded=None)
    return direct_probe or table_probe  # type: ignore[return-value]


def read_lock(path: Path) -> LockRecord | None:
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise DeploymentError(f"daemon lock is not a regular file: {path}")
    try:
        payload = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DeploymentError(f"daemon lock is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("pid"), int):
        raise DeploymentError(f"daemon lock has no integer pid: {path}")
    return LockRecord(
        pid=payload["pid"],
        listen=payload.get("listen"),
        server_url=payload.get("serverUrl"),
        ready=payload.get("ready"),
    )


def classify(paths: DeployPaths, deps: Deps) -> Classification:
    job = query_job(deps)
    default_lock = read_lock(paths.default_lock)
    legacy_lock = read_lock(paths.legacy_lock)
    default_live = default_lock is not None and deps.pid_alive(default_lock.pid)
    legacy_live = legacy_lock is not None and deps.pid_alive(legacy_lock.pid)

    if job.loaded is None:
        kind = STATE_UNVERIFIED
    elif not job.loaded:
        kind = STATE_FOREIGN if (default_live or legacy_live) else (STATE_STALE if (default_lock or legacy_lock) else STATE_ABSENT)
    elif job.pid is None:
        kind = STATE_LOADED_NO_PID
    elif default_live and default_lock is not None and default_lock.pid == job.pid:
        kind = STATE_OURS
    elif legacy_live and legacy_lock is not None and legacy_lock.pid == job.pid:
        kind = STATE_OURS_LEGACY
    elif default_live or legacy_live:
        kind = STATE_FOREIGN
    else:
        kind = STATE_LOADED_STALE_LOCK
    return Classification(kind, job, default_lock, legacy_lock, default_live, legacy_live)


def _run(argv: Sequence[str], deps: Deps) -> CommandResult:
    return deps.run(argv)


def _bootout(paths: DeployPaths, deps: Deps) -> CommandResult:
    return _run(["launchctl", "bootout", f"gui/{deps.uid}/{LABEL}"], deps)


def _job_still_loaded(deps: Deps) -> bool:
    """True when launchd cannot be shown to have stopped the job."""
    probe = query_job(deps)
    return probe.loaded is not False


def _bootstrap_with_retry(paths: DeployPaths, deps: Deps) -> str | None:
    """Bootstrap the deployed plist; return an error string after exhaustion.

    ``launchctl bootstrap`` fails transiently with "5: Input/output error" when
    the GUI domain is not ready (see docs/findings/2026-08.md). Retry with a
    short backoff, mirroring hindsight-blue-green-restart.sh.
    """
    domain = f"gui/{deps.uid}"
    last_error = ""
    for attempt in range(BOOTSTRAP_ATTEMPTS):
        result = _run(["launchctl", "bootstrap", domain, str(paths.plist)], deps)
        if result.returncode == 0:
            return None
        last_error = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        if attempt < BOOTSTRAP_ATTEMPTS - 1:
            deps.sleep(BOOTSTRAP_BACKOFF_SECONDS[min(attempt, len(BOOTSTRAP_BACKOFF_SECONDS) - 1)])
    return last_error


# ---------------------------------------------------------------------------
# Install


def install(
    paths: DeployPaths,
    deps: Deps,
    *,
    restart: bool = False,
    allow_empty_home: bool = False,
    out: Callable[[str], None] = print,
) -> int:
    _require_macos(deps)
    _reject_caller_home(paths)
    template_text = _read_template(paths)
    _validate_home_text(str(paths.home))
    if paths.default_home.is_symlink():
        raise DeploymentError(f"refusing a symlinked default home: {paths.default_home}")
    _require_native_binary(paths)
    _reject_container_job(deps)
    created_home = False
    if not paths.default_home.exists():
        if not allow_empty_home:
            raise DeploymentError(
                f"default zg home is missing: {paths.default_home}; "
                "run once with --allow-empty-home to create it on a new machine"
            )
        _create_default_home(paths, deps)
        created_home = True
    _validate_home_permissions(paths, deps)
    if not allow_empty_home and not _has_workspaces(paths.default_home / "workspaces.json"):
        raise DeploymentError(
            f"default zg home has no usable workspaces.json: {paths.default_home / 'workspaces.json'}; "
            "refusing to adopt a home that is not the populated one (use --allow-empty-home for a new machine)"
        )

    previous_manifest = load_manifest(paths.manifest)
    manifest = previous_manifest
    rendered = render_plist(template_text, paths)
    render_hash = sha256(rendered)
    deployed = _read_plist_bytes(paths.plist)
    deployed_hash = sha256(deployed) if deployed is not None else None
    state = classify(paths, deps)
    _require_install_state(state, restart=restart)

    if deployed is not None and not _is_ours(manifest, deployed_hash) and state.job.loaded and not restart:
        raise DeploymentError(
            f"deployed plist is unmanaged: {paths.plist}; run install --restart to adopt it in a change window"
        )

    if deployed is not None and deployed_hash != render_hash:
        if not restart:
            owner = "managed but changed" if _is_ours(manifest, deployed_hash) else "unmanaged"
            raise DeploymentError(
                f"deployed plist is {owner} and differs from the render; "
                "run install --restart inside a change window"
            )

    if deployed_hash == render_hash and state.kind == STATE_OURS and _is_ours(manifest, deployed_hash):
        if manifest.bootstrapped_render_sha != deployed_hash:
            raise DeploymentError(
                "the loaded job is older than the deployed plist; run install --restart to converge it"
            )
        if not restart:
            out(f"unchanged: {paths.plist} already matches the render and is loaded")
            return 0

    _reject_foreign_listener(paths, state, deps)

    backup = _backup_plist(paths, deployed, deps) if deployed is not None else None
    _atomic_write(paths.plist, rendered, 0o644)
    manifest = Manifest(
        label=LABEL,
        template_sha256=sha256(template_text.encode("utf-8")),
        render_sha256=render_hash,
        rendered_at=deps.now().astimezone(timezone.utc).isoformat(),
        bootstrapped_render_sha=None,
        created_home=created_home or (manifest.created_home if manifest else False),
    )
    _write_manifest(paths, manifest)

    if state.job.loaded:
        bootout = _bootout(paths, deps)
        if bootout.returncode != 0 and _job_still_loaded(deps):
            detail = bootout.stderr.strip() or f"exit {bootout.returncode}"
            _restore_previous_files(paths, deps, backup, deployed, previous_manifest)
            raise DeploymentError(
                f"launchctl bootout failed: {detail}. The running job and its plist were left untouched."
            )
    error = _bootstrap_with_retry(paths, deps)
    if error is not None:
        status = _rollback_failed_install(paths, deps, backup, deployed, previous_manifest, manifest)
        raise DeploymentError(f"launchctl bootstrap failed: {error}. {status}")

    manifest = replace(manifest, bootstrapped_render_sha=render_hash)
    _write_manifest(paths, manifest)
    out(f"installed: {paths.plist}")
    return verify(paths, deps, out=out)


def _restored_manifest(
    previous: Manifest | None, restored_hash: str, deps: Deps
) -> Manifest:
    """Describe a restored deployment truthfully, including its own render hash."""
    if previous is not None and previous.render_sha256 == restored_hash:
        return replace(previous, bootstrapped_render_sha=restored_hash, uninstalled_at=None)
    return Manifest(
        label=LABEL,
        template_sha256=previous.template_sha256 if previous else "",
        render_sha256=restored_hash,
        rendered_at=deps.now().astimezone(timezone.utc).isoformat(),
        bootstrapped_render_sha=restored_hash,
        created_home=previous.created_home if previous else False,
    )


def _restore_previous_files(
    paths: DeployPaths,
    deps: Deps,
    backup: Path | None,
    deployed: bytes | None,
    previous: Manifest | None,
) -> None:
    if backup is not None and deployed is not None:
        _atomic_write(paths.plist, deployed, 0o644)
        if previous is not None:
            _write_manifest(paths, replace(previous, uninstalled_at=None))
        elif os.path.lexists(paths.manifest):
            paths.manifest.unlink()
        return
    if os.path.lexists(paths.plist):
        paths.plist.unlink()
    if previous is not None:
        _write_manifest(paths, replace(previous, uninstalled_at=deps.now().astimezone(timezone.utc).isoformat()))
    elif os.path.lexists(paths.manifest):
        paths.manifest.unlink()


def _rollback_failed_install(
    paths: DeployPaths,
    deps: Deps,
    backup: Path | None,
    deployed: bytes | None,
    previous: Manifest | None,
    failed: Manifest,
) -> str:
    _bootout(paths, deps)
    if backup is not None and deployed is not None:
        restored_hash = sha256(deployed)
        _atomic_write(paths.plist, deployed, 0o644)
        _write_manifest(paths, _restored_manifest(previous, restored_hash, deps))
        restore_error = _bootstrap_with_retry(paths, deps)
        if restore_error is not None:
            return f"Restored the previous plist from {backup}, but bootstrapping it failed: {restore_error}"
        return f"Restored the previous plist from {backup}."
    if os.path.lexists(paths.plist):
        paths.plist.unlink()
    _write_manifest(paths, replace(failed, uninstalled_at=deps.now().astimezone(timezone.utc).isoformat()))
    return "Fresh install failed; no plist was left behind and daemon state was not changed."


def _require_install_state(state: Classification, *, restart: bool) -> None:
    if state.kind == STATE_UNVERIFIED:
        raise DeploymentError(
            "cannot verify launchd job state; refusing to guess. "
            f"Check manually: launchctl print gui/$(id -u)/{LABEL}"
        )
    if state.kind == STATE_FOREIGN:
        raise DeploymentError(
            "a daemon that is not this launchd job owns zvec-grep state; refusing to attach to a foreign home"
        )
    if state.kind in (STATE_LOADED_NO_PID, STATE_LOADED_STALE_LOCK):
        raise DeploymentError(f"launchd job is {state.kind}; run install --restart to converge it")
    if state.kind == STATE_OURS_LEGACY and not restart:
        raise DeploymentError(
            "the running daemon still uses the legacy private home; the cutover requires install --restart"
        )


def _reject_foreign_listener(paths: DeployPaths, state: Classification, deps: Deps) -> None:
    host, port = _split_listen(DEFAULT_LISTEN)
    if not deps.port_open(host, port):
        return
    if state.kind in (STATE_OURS, STATE_OURS_LEGACY):
        return
    raise DeploymentError(
        f"{DEFAULT_LISTEN} is already answered by a listener that is not the {LABEL} job; "
        "refusing to start a second daemon on the same port"
    )


def _require_macos(deps: Deps) -> None:
    if deps.platform != "darwin":
        raise DeploymentError("the native zvec-grep LaunchAgent is macOS-only (launchd); nothing was changed")


def _reject_caller_home(paths: DeployPaths) -> None:
    caller = os.environ.get("ZVEC_GREP_HOME")
    if caller is None:
        return
    if Path(caller).expanduser().resolve() != paths.default_home.resolve():
        raise DeploymentError(
            f"ZVEC_GREP_HOME={caller} points away from the default home {paths.default_home}; "
            "unset it (or point it at the default home) before installing"
        )


def _read_template(paths: DeployPaths) -> str:
    if not paths.template.is_file():
        raise DeploymentError(f"launchd template not found: {paths.template}")
    return paths.template.read_text(encoding="utf-8")


def _require_native_binary(paths: DeployPaths) -> None:
    binary = paths.binary
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise DeploymentError(f"native zg binary is missing or not executable: {binary}")


def _reject_container_job(deps: Deps) -> None:
    result = _run(["launchctl", "list", CONTAINER_LABEL], deps)
    if result.returncode == 0:
        raise DeploymentError(
            f"container job {CONTAINER_LABEL} is loaded and also binds loopback port 7999; "
            "unload it before adopting the native daemon"
        )


def _create_default_home(paths: DeployPaths, deps: Deps) -> None:
    parent = paths.default_home.parent
    parent_stats = parent.stat()
    if parent_stats.st_uid != deps.uid:
        raise DeploymentError(f"refusing to create {paths.default_home}: parent is not owned by the invoking user")
    paths.default_home.mkdir(mode=0o700)
    os.chmod(paths.default_home, 0o700)


def _validate_home_permissions(paths: DeployPaths, deps: Deps) -> None:
    stats = paths.default_home.stat()
    if stats.st_uid != deps.uid:
        raise DeploymentError(f"default zg home is not owned by uid {deps.uid}: {paths.default_home}")
    if stats.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise DeploymentError(f"default zg home is group/world writable: {paths.default_home}")


def _has_workspaces(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("workspaces"))


def _split_listen(listen: str) -> tuple[str, int]:
    host, _, port_text = listen.rpartition(":")
    if not host or not port_text.isdigit():
        raise DeploymentError(f"malformed listen address: {listen}")
    return host, int(port_text)


# ---------------------------------------------------------------------------
# Verify


def verify(paths: DeployPaths, deps: Deps, *, out: Callable[[str], None] = print) -> int:
    _require_macos(deps)
    state = classify(paths, deps)
    out(f"state: {state.kind}")
    if state.kind == STATE_OURS_LEGACY:
        raise DeploymentError(
            "daemon is running under the legacy private home "
            f"{paths.legacy_home}; cutover not performed — run install --restart"
        )
    if state.kind in (STATE_STALE, STATE_ABSENT):
        raise DeploymentError(
            f"no daemon state in {paths.default_home}; the daemon has not adopted the default home"
        )
    if state.kind != STATE_OURS:
        raise DeploymentError(f"deployed daemon is not ready under the default home (state: {state.kind})")

    manifest = load_manifest(paths.manifest)
    if manifest is None or manifest.uninstalled_at is not None:
        raise DeploymentError("deployment manifest is missing; run install --restart to adopt this deployment")
    deployed = _read_plist_bytes(paths.plist)
    if deployed is None:
        raise DeploymentError(f"deployed plist is missing: {paths.plist}")
    deployed_hash = sha256(deployed)
    if deployed_hash != manifest.render_sha256:
        raise DeploymentError("deployed plist does not match the recorded render; run install --restart")
    parsed = plistlib.loads(deployed)
    if "ZVEC_GREP_HOME" in (parsed.get("EnvironmentVariables") or {}):
        raise DeploymentError("deployed plist sets ZVEC_GREP_HOME; configuration integrity check failed")

    rendered = render_plist(_read_template(paths), paths)
    if sha256(rendered) != manifest.render_sha256:
        raise DeploymentError("the repository template changed since install; run install --restart in a change window")
    if manifest.bootstrapped_render_sha != deployed_hash:
        raise DeploymentError("deployed plist is newer than the loaded job; run install --restart")

    lock = state.default_lock
    if lock is None:  # pragma: no cover - STATE_OURS guarantees a live default-home lock
        raise DeploymentError("daemon lock is missing under the default home")
    if lock.listen != DEFAULT_LISTEN:
        raise DeploymentError(f"daemon lock reports listen={lock.listen!r}, expected {DEFAULT_LISTEN}")
    if lock.ready is not True:
        raise DeploymentError("daemon lock is not ready")
    if state.job.pid != lock.pid:  # pragma: no cover - state OURS already proves pid equality
        raise DeploymentError("loaded job pid does not match the default-home instance lock")

    environment = dict(parsed.get("EnvironmentVariables") or {})
    program = (parsed.get("ProgramArguments") or [""])[0]
    result = deps.exec_clean([program, "--server", "status", "--check-ready"], environment)
    if result.returncode != 0 or not re.search(r"(?im)^\s*Server:\s*ready\s*$", result.stdout):
        detail = (result.stderr or result.stdout).strip()
        raise DeploymentError(f"clean-environment zg --server status is not ready under the default home: {detail}")

    resolved = deps.which("zg")
    if resolved is not None and Path(resolved).resolve() != Path(program).resolve():
        raise DeploymentError(
            f"PATH resolves zg to {resolved}, not the deployed binary {program}; "
            "remove or repoint that shim so the CLI and the daemon share one home"
        )
    if resolved is None:
        out(f"warning: no zg on PATH; add {paths.native_root / 'bin'} to PATH for CLI use")

    out(f"home: {paths.default_home}")
    out(f"lock: {paths.default_lock}")
    out(f"workspaces: {_workspace_count(paths.default_home / 'workspaces.json')}")
    out("verify: ok")
    return 0


def _workspace_count(path: Path) -> int:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return 0
    workspaces = payload.get("workspaces") if isinstance(payload, dict) else None
    if isinstance(workspaces, dict):
        return len(workspaces)
    if isinstance(workspaces, list):
        return len(workspaces)
    return 0


# ---------------------------------------------------------------------------
# Uninstall and lock repair


def uninstall(
    paths: DeployPaths,
    deps: Deps,
    *,
    force_unmanaged: bool = False,
    out: Callable[[str], None] = print,
) -> int:
    _require_macos(deps)
    manifest = load_manifest(paths.manifest)
    deployed = _read_plist_bytes(paths.plist)
    state = classify(paths, deps)
    if state.kind == STATE_UNVERIFIED:
        raise DeploymentError(f"cannot verify launchd state; remove manually: launchctl bootout gui/{deps.uid}/{LABEL}")
    if deployed is None:
        out(f"nothing to remove: {paths.plist} is not deployed")
        _stop_job_or_fail(paths, deps, state)
        _mark_uninstalled(paths, manifest, deps)
        if _remove_created_empty_home(paths, manifest):
            out(f"removed empty home created by install: {paths.default_home}")
        return 0
    if not _is_ours(manifest, sha256(deployed)) and not force_unmanaged:
        raise DeploymentError(
            f"deployed plist at {paths.plist} was not installed by this tool; "
            "refusing to remove it (use --force-unmanaged to override)"
        )
    _stop_job_or_fail(paths, deps, state)
    if os.path.lexists(paths.plist):
        paths.plist.unlink()
    _mark_uninstalled(paths, manifest, deps)
    home_removed = _remove_created_empty_home(paths, manifest)
    out(f"removed: {paths.plist}")
    out(f"preserved: {paths.default_home} (state, workspaces.json, indexes)")
    if paths.backups.is_dir():
        out(f"preserved backup directory: {paths.backups} (rollback copies)")
    if home_removed:
        out(f"removed empty home created by install: {paths.default_home}")
    return 0


def _stop_job_or_fail(paths: DeployPaths, deps: Deps, state: Classification) -> None:
    """Boot the job out, and refuse to claim success if launchd still runs it."""
    if state.job.loaded is False:
        return
    bootout = _bootout(paths, deps)
    if _job_still_loaded(deps):
        detail = bootout.stderr.strip() or f"exit {bootout.returncode}"
        raise DeploymentError(
            f"launchctl bootout did not stop {LABEL}: {detail}; the plist and the running daemon were left in place"
        )


def _mark_uninstalled(paths: DeployPaths, manifest: Manifest | None, deps: Deps) -> None:
    if manifest is None:
        return
    _write_manifest(paths, replace(manifest, uninstalled_at=deps.now().astimezone(timezone.utc).isoformat()))


def _remove_created_empty_home(paths: DeployPaths, manifest: Manifest | None) -> bool:
    """Remove a home install created, but only when it holds no user data.

    The manifest and backups are this tool's artefacts; workspaces.json,
    indexes or anything else the user added keep the home in place.
    """
    if manifest is None or not manifest.created_home or not paths.default_home.is_dir():
        return False
    unrelated = [entry for entry in paths.default_home.iterdir() if entry.name != "deploy"]
    if unrelated:
        return False
    if paths.backups.is_dir():
        try:
            paths.backups.rmdir()
        except OSError:
            return False
    if paths.manifest.exists():
        paths.manifest.unlink()
    deploy_dir = paths.default_home / "deploy"
    if deploy_dir.is_dir():
        try:
            deploy_dir.rmdir()
        except OSError:
            return False
    try:
        paths.default_home.rmdir()
    except OSError:
        return False
    return True


def lock_repair(
    paths: DeployPaths,
    deps: Deps,
    *,
    confirm: bool,
    out: Callable[[str], None] = print,
) -> int:
    """Rename a stale default-home lock; the legacy home is not ours to repair."""
    _require_macos(deps)
    if not confirm:
        raise DeploymentError("lock-repair requires --confirm-dead-lock after you have inspected the pid")
    if not os.path.lexists(paths.default_lock):
        out(f"nothing to repair: {paths.default_lock} does not exist")
        return 0
    job = query_job(deps)
    if job.loaded is None:
        raise DeploymentError(f"cannot verify launchd job state; check: launchctl print gui/{deps.uid}/{LABEL}")
    if job.loaded:
        raise DeploymentError(
            "launchd job is still loaded; stop it first (launchctl bootout "
            f"gui/{deps.uid}/{LABEL}), re-check the pid, then run lock-repair again"
        )
    try:
        lock = read_lock(paths.default_lock)
    except DeploymentError:
        # A corrupt lock cannot name a pid; --confirm-dead-lock is the
        # operator's assertion, and the file is still preserved by renaming.
        lock = None
    if lock is not None and deps.pid_alive(lock.pid):
        raise DeploymentError(f"lock pid {lock.pid} is still alive; refusing to rename a live lock")
    target = _free_path(paths.default_lock.with_name(f"instance.lock.stale-{_utc_stamp(deps.now())}"))
    os.rename(paths.default_lock, target)
    out(f"renamed stale lock to {target}")
    return 0


# ---------------------------------------------------------------------------
# CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home(), help="deploying user's home directory")
    parser.add_argument("--template", type=Path, default=None, help="path to the launchd plist template")
    subcommands = parser.add_subparsers(dest="command", required=True)

    install_parser = subcommands.add_parser("install", help="render, deploy and verify the LaunchAgent")
    install_parser.add_argument("--restart", action="store_true", help="replace or converge a loaded job")
    install_parser.add_argument(
        "--allow-empty-home",
        action="store_true",
        help="create an absent default home (mode 0700) on a new machine",
    )

    subcommands.add_parser("verify", help="prove the deployed daemon uses the default home")

    uninstall_parser = subcommands.add_parser("uninstall", help="remove only the plist this tool installed")
    uninstall_parser.add_argument(
        "--force-unmanaged",
        action="store_true",
        help="bootout and remove a plist this tool did not install",
    )

    repair_parser = subcommands.add_parser("lock-repair", help="rename a provably dead default-home lock")
    repair_parser.add_argument("--confirm-dead-lock", action="store_true", help="confirm you inspected the dead pid")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    paths = resolve_paths(arguments.home, arguments.template)
    deps = Deps()
    try:
        if arguments.command == "install":
            return install(
                paths,
                deps,
                restart=arguments.restart,
                allow_empty_home=arguments.allow_empty_home,
            )
        if arguments.command == "verify":
            return verify(paths, deps)
        if arguments.command == "uninstall":
            return uninstall(paths, deps, force_unmanaged=arguments.force_unmanaged)
        return lock_repair(paths, deps, confirm=arguments.confirm_dead_lock)
    except DeploymentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
