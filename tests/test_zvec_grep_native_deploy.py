"""Behavioural coverage for the native zvec-grep default-home deployment (#249).

Each test maps to a control objective: configuration integrity and change
management (SOC 2 CC8.1, FedRAMP CM-3/CM-6), least privilege and symlink
safety (AC-6), recovery and continuity (CP-10), and auditability of the
install/verify/uninstall lifecycle.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from engram.deploy import zvec_grep_native as deploy

REPO_TEMPLATE = Path(__file__).parents[1] / "launchd" / f"{deploy.LABEL}.plist"
READY = deploy.CommandResult(0, "Server: ready\nPID: 4321\nURL: http://127.0.0.1:7999/mcp\n")
NOT_READY = deploy.CommandResult(1, "Server: stopped\n")


class FakeLaunchd:
    """A launchd double: it owns a job and, like zg, writes the instance lock."""

    def __init__(self, paths: deploy.DeployPaths) -> None:
        self.paths = paths
        self.loaded = False
        self.pid: int | None = None
        self.container_loaded = False
        self.job_loaded_without_pid = False
        self.failed_bootstraps = 0
        self.failed_bootouts = 0
        self.next_pid = 4321
        self.spawned: set[int] = set()
        self.calls: list[list[str]] = []

    def fail_next_bootstraps(self, count: int) -> None:
        self.failed_bootstraps = count

    def set_loaded(self, pid: int | None) -> None:
        self.loaded = True
        self.pid = pid

    def __call__(self, argv):
        self.calls.append(list(argv))
        arguments = list(argv[1:])
        if arguments == ["list", deploy.CONTAINER_LABEL]:
            return deploy.CommandResult(0 if self.container_loaded else 1)
        if arguments == ["list", deploy.LABEL]:
            if not self.loaded:
                return deploy.CommandResult(3, "", "Could not find service")
            if self.pid is None:
                return deploy.CommandResult(0, '{\n\t"Label" = "x";\n}')
            return deploy.CommandResult(0, f'"PID" = {self.pid};')
        if arguments == ["list"]:
            if not self.loaded:
                return deploy.CommandResult(0, "1\t0\tcom.apple.other\n")
            pid_text = str(self.pid) if self.pid is not None else "-"
            return deploy.CommandResult(0, f"{pid_text}\t0\t{deploy.LABEL}\n")
        if arguments[0] == "bootout":
            if self.failed_bootouts:
                self.failed_bootouts -= 1
                return deploy.CommandResult(1, "", "Bootout failed: 5: Input/output error")
            if self.pid is not None:
                self.spawned.discard(self.pid)
            self.loaded = False
            self.pid = None
            return deploy.CommandResult(0)
        if arguments[0] == "bootstrap":
            if self.failed_bootstraps:
                self.failed_bootstraps -= 1
                return deploy.CommandResult(5, "", "Bootstrap failed: 5: Input/output error")
            self.loaded = True
            self.pid = self.next_pid
            self.spawned.add(self.next_pid)
            self.write_lock(self.pid)
            return deploy.CommandResult(0)
        return deploy.CommandResult(1, "", f"unexpected launchctl call: {arguments}")

    def write_lock(self, pid: int, *, listen: str = deploy.DEFAULT_LISTEN, ready: bool = True) -> None:
        path = self.paths.default_lock
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": pid, "listen": listen, "serverUrl": f"http://{listen}/mcp", "ready": ready}))

    def bootout_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:2] == ["bootout"]]

    def bootstrap_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:2] == ["bootstrap"]]


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.paths = deploy.resolve_paths(self.home, REPO_TEMPLATE)
        self.launchd = FakeLaunchd(self.paths)
        self.alive: set[int] = set()
        self.port_open = False
        self.env_calls: list[tuple[list[str], dict[str, str]]] = []
        self.exec_result = READY
        self.which_result: str | None = str(self.paths.binary)
        self.now = deploy.datetime(2026, 11, 26, 12, 0, tzinfo=deploy.timezone.utc)

    def prepare(self, *, populated: bool = True) -> "Harness":
        (self.paths.native_root / "bin").mkdir(parents=True, exist_ok=True)
        self.paths.binary.write_text("#!/bin/sh\n")
        self.paths.binary.chmod(0o755)
        self.paths.default_home.mkdir(parents=True, exist_ok=True)
        self.paths.model_cache.mkdir(parents=True, exist_ok=True)
        if populated:
            (self.paths.default_home / "workspaces.json").write_text(
                json.dumps({"version": 1, "workspaces": {"a": {"root": "/x"}}})
            )
        return self

    def deps(self) -> deploy.Deps:
        def exec_clean(argv, env):
            self.env_calls.append((list(argv), dict(env)))
            return self.exec_result

        return deploy.Deps(
            run=self.launchd,
            exec_clean=exec_clean,
            pid_alive=lambda pid: pid in self.alive or pid in self.launchd.spawned,
            port_open=lambda host, port: self.port_open,
            which=lambda name: self.which_result,
            now=lambda: self.now,
            sleep=lambda seconds: None,
            uid=os.getuid(),
            platform="darwin",
        )

    def install(self, **kwargs):
        return deploy.install(self.paths, self.deps(), **kwargs)

    def verify(self):
        return deploy.verify(self.paths, self.deps())


@pytest.fixture()
def harness(tmp_path) -> Harness:
    return Harness(tmp_path).prepare()


def _managed_install(harness: Harness) -> None:
    harness.launchd.set_loaded(None)
    harness.launchd.loaded = False
    harness.launchd.pid = None
    assert harness.install(allow_empty_home=False) == 0


# ---------------------------------------------------------------------------
# Configuration integrity and change management (CM-3, CM-6, CC8.1)


def test_render_preserves_comments_and_omits_home_env(tmp_path):
    home = tmp_path / "a&b"
    paths = deploy.resolve_paths(home, REPO_TEMPLATE)

    rendered = deploy.render_plist(REPO_TEMPLATE.read_text(), paths)

    assert rendered == deploy.render_plist(REPO_TEMPLATE.read_text(), paths)
    assert b"<!--" in rendered and b"Engram's native zvec-grep daemon" in rendered
    parsed = deploy.plistlib.loads(rendered)
    assert parsed["Label"] == REPO_TEMPLATE.stem == deploy.LABEL
    assert "ZVEC_GREP_HOME" not in parsed["EnvironmentVariables"]
    assert parsed["EnvironmentVariables"]["HOME"] == str(home)
    assert parsed["SoftResourceLimits"]["NumberOfFiles"] == 65536
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["RunAtLoad"] is True
    assert "&amp;" in rendered.decode()


def test_install_writes_once_and_is_idempotent(harness):
    _managed_install(harness)
    plist_mtime = harness.paths.plist.stat().st_mtime_ns
    bootstrap_count = len(harness.launchd.bootstrap_calls())
    backups = list(harness.paths.backups.glob("*")) if harness.paths.backups.exists() else []

    messages: list[str] = []
    assert deploy.install(harness.paths, harness.deps(), out=messages.append) == 0

    assert any("unchanged" in message for message in messages)
    assert harness.paths.plist.stat().st_mtime_ns == plist_mtime
    assert len(harness.launchd.bootstrap_calls()) == bootstrap_count
    assert harness.launchd.bootout_calls() == []
    assert (list(harness.paths.backups.glob("*")) if harness.paths.backups.exists() else []) == backups


def test_install_refuses_foreign_caller_home(harness, monkeypatch):
    monkeypatch.setenv("ZVEC_GREP_HOME", "/somewhere/else")

    with pytest.raises(deploy.DeploymentError, match="points away from the default home"):
        harness.install()

    assert not harness.paths.plist.exists()


def test_install_refuses_container_job_on_the_same_port(harness):
    harness.launchd.container_loaded = True

    with pytest.raises(deploy.DeploymentError, match="container job"):
        harness.install()


# ---------------------------------------------------------------------------
# Least privilege and symlink safety (AC-6)


def test_symlinked_home_and_plist_are_refused(harness, tmp_path):
    real = tmp_path / "real-home"
    real.mkdir()
    shutil.rmtree(harness.paths.default_home)
    harness.paths.default_home.symlink_to(real)

    with pytest.raises(deploy.DeploymentError, match="symlinked default home"):
        harness.install()

    harness.paths.default_home.unlink()
    harness.paths.default_home.mkdir()
    (harness.paths.default_home / "workspaces.json").write_text(
        json.dumps({"version": 1, "workspaces": {"a": {"root": "/x"}}})
    )
    target = tmp_path / "target.plist"
    target.write_text("do not touch")
    harness.paths.plist.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.plist.symlink_to(target)

    with pytest.raises(deploy.DeploymentError, match="symlinked plist"):
        harness.install()

    assert target.read_text() == "do not touch"


def test_empty_home_requires_opt_in_and_created_dir_is_private(harness):
    shutil.rmtree(harness.paths.default_home)
    harness.paths.default_home.parent.chmod(0o755)

    with pytest.raises(deploy.DeploymentError, match="default zg home is missing"):
        harness.install()

    assert harness.install(allow_empty_home=True) == 0
    assert stat.S_IMODE(harness.paths.default_home.stat().st_mode) == 0o700


def test_existing_home_mode_is_never_altered(harness):
    harness.paths.default_home.chmod(0o755)
    (harness.paths.default_home / "workspaces.json").unlink()

    assert harness.install(allow_empty_home=True) == 0
    assert stat.S_IMODE(harness.paths.default_home.stat().st_mode) == 0o755


def test_missing_workspaces_registry_fails_closed(harness):
    (harness.paths.default_home / "workspaces.json").write_text("{}")

    with pytest.raises(deploy.DeploymentError, match="workspaces.json"):
        harness.install()


# ---------------------------------------------------------------------------
# Duplicate daemon / port conflict / stale locks (CM-3, SC-7)


def test_foreign_listener_refused_but_legacy_job_is_the_expected_cutover(harness):
    harness.port_open = True
    with pytest.raises(deploy.DeploymentError, match="second daemon"):
        harness.install()

    harness.launchd.set_loaded(24624)
    harness.paths.legacy_lock.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.legacy_lock.write_text(json.dumps({"pid": 24624, "listen": deploy.DEFAULT_LISTEN}))
    harness.alive = {24624}

    assert harness.install(restart=True) == 0
    assert harness.launchd.bootout_calls()


def test_loaded_job_without_pid_or_stale_lock_requires_restart(harness):
    harness.launchd.set_loaded(None)
    with pytest.raises(deploy.DeploymentError, match="loaded-no-pid"):
        harness.install()

    harness.launchd.set_loaded(999)
    with pytest.raises(deploy.DeploymentError, match="loaded-stale-lock"):
        harness.install()
    assert not harness.paths.plist.exists()


def test_legacy_private_home_requires_explicit_cutover(harness):
    harness.launchd.set_loaded(24624)
    harness.paths.legacy_lock.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.legacy_lock.write_text(json.dumps({"pid": 24624}))
    harness.alive = {24624}

    with pytest.raises(deploy.DeploymentError, match="legacy private home"):
        harness.install()

    with pytest.raises(deploy.DeploymentError, match="legacy private home"):
        harness.verify()


# ---------------------------------------------------------------------------
# Recovery and rollback (CP-10, CC8.1)


def test_bootstrap_retry_succeeds_after_transient_error(harness):
    harness.launchd.set_loaded(None)
    harness.launchd.loaded = False
    harness.launchd.fail_next_bootstraps(2)

    assert harness.install() == 0
    assert len(harness.launchd.bootstrap_calls()) == 3


def test_bootstrap_exhaustion_restores_the_previous_plist(harness):
    _managed_install(harness)
    previous = harness.paths.plist.read_bytes()
    harness.launchd.fail_next_bootstraps(3)

    with pytest.raises(deploy.DeploymentError, match="Restored the previous plist"):
        harness.install(restart=True)

    assert harness.paths.plist.read_bytes() == previous
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None and manifest.bootstrapped_render_sha == deploy.sha256(previous)


def test_fresh_install_failure_leaves_the_host_as_found(harness):
    harness.launchd.set_loaded(None)
    harness.launchd.loaded = False
    harness.launchd.fail_next_bootstraps(99)

    with pytest.raises(deploy.DeploymentError, match="no plist was left behind"):
        harness.install()

    assert not harness.paths.plist.exists()
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None and manifest.uninstalled_at is not None


def test_install_without_restart_never_replaces_a_loaded_job(harness):
    _managed_install(harness)
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None
    older = b"<?xml version='1.0'?><plist version='1.0'><dict/></plist>"
    harness.paths.plist.write_bytes(older)
    deploy._write_manifest(harness.paths, deploy.replace(manifest, render_sha256=deploy.sha256(older)))

    with pytest.raises(deploy.DeploymentError, match="differs from the render"):
        harness.install()

    assert harness.paths.plist.read_bytes() == older
    assert harness.launchd.bootout_calls() == []


# ---------------------------------------------------------------------------
# Installation auditability (CM-3, AC-6)


def test_manifest_absent_means_unmanaged(harness):
    _managed_install(harness)
    harness.paths.manifest.unlink()

    with pytest.raises(deploy.DeploymentError, match="unmanaged"):
        harness.install()

    assert harness.launchd.bootout_calls() == []


# ---------------------------------------------------------------------------
# Verify: evidence-based readiness and the #249 regression gate


def test_verify_names_legacy_and_recycled_lock_states(harness):
    harness.launchd.set_loaded(24624)
    harness.paths.legacy_lock.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.legacy_lock.write_text(json.dumps({"pid": 24624}))
    harness.alive = {24624}
    with pytest.raises(deploy.DeploymentError, match="legacy private home"):
        harness.verify()

    harness.paths.legacy_lock.unlink()
    harness.launchd.set_loaded(111)
    harness.paths.default_lock.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.default_lock.write_text(json.dumps({"pid": 111, "listen": deploy.DEFAULT_LISTEN, "ready": True}))
    harness.alive = set()
    with pytest.raises(deploy.DeploymentError, match="loaded-stale-lock"):
        harness.verify()


def test_verify_rejects_unverified_and_foreign_jobs(harness):
    harness.launchd.loaded = False
    harness.paths.default_lock.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.default_lock.write_text(json.dumps({"pid": 55, "listen": deploy.DEFAULT_LISTEN, "ready": True}))
    harness.alive = {55}
    with pytest.raises(deploy.DeploymentError, match="foreign"):
        harness.verify()

    unreadable = deploy.replace(
        harness.deps(), run=lambda argv: deploy.CommandResult(1, "", "permission denied")
    )
    with pytest.raises(deploy.DeploymentError, match="cannot verify launchd job state"):
        deploy.install(harness.paths, unreadable)
    with pytest.raises(deploy.DeploymentError, match="unverified"):
        deploy.verify(harness.paths, unreadable)


def test_verify_fails_when_the_loaded_job_is_older_than_the_plist(harness):
    _managed_install(harness)
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None
    deploy._write_manifest(harness.paths, deploy.replace(manifest, bootstrapped_render_sha="0" * 64))

    with pytest.raises(deploy.DeploymentError, match="newer than the loaded job"):
        harness.verify()


def test_verify_fails_on_recorded_render_drift(harness):
    _managed_install(harness)
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None
    deploy._write_manifest(harness.paths, deploy.replace(manifest, render_sha256="1" * 64))

    with pytest.raises(deploy.DeploymentError, match="recorded render"):
        harness.verify()


def test_verify_path_zg_regression_and_env_contract(harness):
    _managed_install(harness)
    harness.env_calls.clear()

    harness.which_result = "/Users/example/bin/zg"
    with pytest.raises(deploy.DeploymentError, match="PATH resolves zg"):
        harness.verify()

    harness.which_result = None
    assert harness.verify() == 0
    _argv, environment = harness.env_calls[-1]
    assert environment == {
        "HOME": str(harness.home),
        "ZVEC_GREP_MODEL_CACHE": str(harness.paths.model_cache),
        "ZVEC_GREP_MCP_SESSION_IDLE_SECONDS": "43200",
    }
    assert "ZVEC_GREP_HOME" not in environment

    harness.env_calls.clear()
    harness.exec_result = NOT_READY
    with pytest.raises(deploy.DeploymentError, match="not ready under the default home"):
        harness.verify()


# ---------------------------------------------------------------------------
# Uninstall and lock repair (AC-6, CM-5)


def test_uninstall_removes_only_the_managed_plist_and_preserves_state(harness):
    _managed_install(harness)
    workspaces = (harness.paths.default_home / "workspaces.json").read_bytes()

    assert deploy.uninstall(harness.paths, harness.deps()) == 0
    assert not harness.paths.plist.exists()
    assert (harness.paths.default_home / "workspaces.json").read_bytes() == workspaces
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None and manifest.uninstalled_at is not None
    assert deploy.uninstall(harness.paths, harness.deps()) == 0


def test_uninstall_refuses_a_foreign_plist_unless_forced(harness):
    harness.paths.plist.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.plist.write_text("foreign")

    with pytest.raises(deploy.DeploymentError, match="not installed by this tool"):
        deploy.uninstall(harness.paths, harness.deps())

    assert harness.paths.plist.read_text() == "foreign"
    assert deploy.uninstall(harness.paths, harness.deps(), force_unmanaged=True) == 0
    assert not harness.paths.plist.exists()


def test_created_home_is_removed_only_when_it_holds_no_user_data(harness):
    shutil.rmtree(harness.paths.default_home)
    assert harness.install(allow_empty_home=True) == 0
    # zg state in a home install created is preserved by default (read-only
    # lock policy); only a home with nothing but our artefacts is removed.
    assert deploy.uninstall(harness.paths, harness.deps()) == 0
    assert harness.paths.default_home.exists()

    assert harness.install(allow_empty_home=True) == 0
    shutil.rmtree(harness.paths.default_home / "daemon")
    assert deploy.uninstall(harness.paths, harness.deps()) == 0
    assert not harness.paths.default_home.exists()

    assert harness.install(allow_empty_home=True) == 0
    (harness.paths.default_home / "workspaces.json").write_text('{"workspaces": {"user": {}}}')
    assert deploy.uninstall(harness.paths, harness.deps()) == 0
    assert (harness.paths.default_home / "workspaces.json").exists()


def test_locks_are_never_mutated_by_install_verify_or_uninstall(harness):
    _managed_install(harness)
    lock_bytes = harness.paths.default_lock.read_bytes()
    lock_mtime = harness.paths.default_lock.stat().st_mtime_ns

    assert harness.verify() == 0
    assert deploy.uninstall(harness.paths, harness.deps()) == 0

    assert harness.paths.default_lock.read_bytes() == lock_bytes
    assert harness.paths.default_lock.stat().st_mtime_ns == lock_mtime


def test_lock_repair_renames_a_dead_lock_and_refuses_unsafe_state(harness):
    harness.paths.default_lock.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.default_lock.write_text(json.dumps({"pid": 777}))

    with pytest.raises(deploy.DeploymentError, match="confirm"):
        deploy.lock_repair(harness.paths, harness.deps(), confirm=False)

    harness.alive = {777}
    with pytest.raises(deploy.DeploymentError, match="still alive"):
        deploy.lock_repair(harness.paths, harness.deps(), confirm=True)

    harness.alive = set()
    harness.launchd.set_loaded(None)
    with pytest.raises(deploy.DeploymentError, match="still loaded"):
        deploy.lock_repair(harness.paths, harness.deps(), confirm=True)

    harness.launchd.loaded = False
    assert deploy.lock_repair(harness.paths, harness.deps(), confirm=True) == 0
    renamed = list(harness.paths.default_lock.parent.glob("instance.lock.stale-*"))
    assert len(renamed) == 1
    assert json.loads(renamed[0].read_text()) == {"pid": 777}
    assert not harness.paths.default_lock.exists()


def test_backup_pruning_keeps_the_newest_five_after_a_successful_write(harness):
    harness.paths.backups.mkdir(parents=True)
    for day in range(1, 7):
        (harness.paths.backups / f"{deploy.LABEL}.plist.2026110{day}T000000Z").write_text(str(day))
    unrelated = harness.paths.backups / "keep-me.txt"
    unrelated.write_text("x")
    harness.paths.plist.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.plist.write_text("deployed")

    new_backup = deploy._backup_plist(harness.paths, b"new", harness.deps())

    assert new_backup is not None
    remaining = sorted(path.name for path in harness.paths.backups.glob(f"{deploy.LABEL}.*"))
    assert len(remaining) == deploy.BACKUP_LIMIT
    assert f"{deploy.LABEL}.plist.20261101T000000Z" not in remaining
    assert unrelated.exists()


# ---------------------------------------------------------------------------
# Configuration integrity: a tampered template or state file is refused


def test_render_rejects_contract_violations_on_the_reviewed_template(harness):
    paths = harness.paths
    good = deploy.plistlib.loads(deploy.render_plist(REPO_TEMPLATE.read_text(), paths))

    def replace_argument(parsed, index, value):
        parsed["ProgramArguments"][index] = value

    mutations = [
        (lambda parsed: parsed.__setitem__("Label", "com.example.other"), "label"),
        (lambda parsed: replace_argument(parsed, 0, "/tmp/zg"), "exec the native zg"),
        (
            lambda parsed: replace_argument(
                parsed, parsed["ProgramArguments"].index("--listen") + 1, "0.0.0.0:7999"
            ),
            "listen on",
        ),
        (lambda parsed: parsed["EnvironmentVariables"].__setitem__("ZVEC_GREP_HOME", "/elsewhere"), "must not set"),
        (lambda parsed: parsed["EnvironmentVariables"].__setitem__("HOME", "/elsewhere"), "pin HOME"),
        (lambda parsed: parsed["EnvironmentVariables"].__setitem__("ZVEC_GREP_MODEL_CACHE", "/elsewhere"), "model cache"),
    ]
    for mutate, match in mutations:
        tampered = copy.deepcopy(good)
        mutate(tampered)
        with pytest.raises(deploy.DeploymentError, match=match):
            deploy.render_plist(deploy.plistlib.dumps(tampered).decode(), paths)

    with pytest.raises(deploy.DeploymentError, match="not valid"):
        deploy.render_plist("<plist", paths)


@pytest.mark.parametrize("bad_home", ["relative/home", "/tmp/with\x01control", "/tmp/__HOME__"])
def test_render_rejects_unsafe_home_values(tmp_path, bad_home):
    paths = deploy.resolve_paths(Path(bad_home), REPO_TEMPLATE)

    with pytest.raises(deploy.DeploymentError, match="HOME"):
        deploy.render_plist(REPO_TEMPLATE.read_text(), paths)


def test_manifest_and_lock_files_are_validated_not_trusted(harness, tmp_path):
    harness.paths.manifest.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.manifest.write_text("not-json")
    with pytest.raises(deploy.DeploymentError, match="not valid JSON"):
        deploy.load_manifest(harness.paths.manifest)

    harness.paths.manifest.write_text("[]")
    with pytest.raises(deploy.DeploymentError, match="JSON object"):
        deploy.load_manifest(harness.paths.manifest)

    target = tmp_path / "manifest-target"
    target.write_text("{}")
    harness.paths.manifest.unlink()
    harness.paths.manifest.symlink_to(target)
    with pytest.raises(deploy.DeploymentError, match="symlinked manifest"):
        deploy.load_manifest(harness.paths.manifest)

    lock = harness.paths.default_lock
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("not-json")
    with pytest.raises(deploy.DeploymentError, match="not valid JSON"):
        deploy.read_lock(lock)
    lock.write_text('{"pid": "4321"}')
    with pytest.raises(deploy.DeploymentError, match="no integer pid"):
        deploy.read_lock(lock)
    lock.unlink()
    lock.symlink_to(target)
    with pytest.raises(deploy.DeploymentError, match="not a regular file"):
        deploy.read_lock(lock)


def test_verify_refuses_missing_manifest_plist_and_tampering(harness, tmp_path):
    _managed_install(harness)
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None

    harness.paths.manifest.unlink()
    with pytest.raises(deploy.DeploymentError, match="manifest is missing"):
        harness.verify()
    deploy._write_manifest(harness.paths, deploy.replace(manifest, uninstalled_at="2026-11-26T00:00:00+00:00"))
    with pytest.raises(deploy.DeploymentError, match="manifest is missing"):
        harness.verify()
    deploy._write_manifest(harness.paths, manifest)

    harness.paths.plist.unlink()
    with pytest.raises(deploy.DeploymentError, match="deployed plist is missing"):
        harness.verify()

    # A repository template that no longer matches the recorded render must not verify.
    alternate = tmp_path / "alternate.plist"
    alternate.write_text(REPO_TEMPLATE.read_text() + "\n<!-- changed -->\n")
    assert harness.install(restart=True) == 0
    alternate_paths = deploy.replace(harness.paths, template=alternate)
    with pytest.raises(deploy.DeploymentError, match="template changed"):
        deploy.verify(alternate_paths, harness.deps())


def test_verify_refuses_a_deployed_plist_that_reintroduces_zvec_grep_home(harness):
    _managed_install(harness)
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None
    parsed = deploy.plistlib.loads(harness.paths.plist.read_bytes())
    parsed["EnvironmentVariables"]["ZVEC_GREP_HOME"] = "/legacy"
    tampered = deploy.plistlib.dumps(parsed)
    deploy._atomic_write(harness.paths.plist, tampered, 0o644)
    deploy._write_manifest(
        harness.paths,
        deploy.replace(
            manifest,
            render_sha256=deploy.sha256(tampered),
            bootstrapped_render_sha=deploy.sha256(tampered),
        ),
    )

    with pytest.raises(deploy.DeploymentError, match="configuration integrity"):
        harness.verify()


def test_verify_refuses_lock_listen_or_readiness_drift(harness):
    _managed_install(harness)
    harness.launchd.write_lock(harness.launchd.pid, listen="127.0.0.1:7000")
    with pytest.raises(deploy.DeploymentError, match="listen="):
        harness.verify()

    harness.launchd.write_lock(harness.launchd.pid, ready=False)
    with pytest.raises(deploy.DeploymentError, match="not ready"):
        harness.verify()


def test_verify_refuses_a_home_the_daemon_never_adopted(harness):
    harness.launchd.loaded = False
    with pytest.raises(deploy.DeploymentError, match="has not adopted the default home"):
        harness.verify()


# ---------------------------------------------------------------------------
# Fail-closed preflight for a missing binary/template, foreign state, perms


def test_install_refuses_missing_template_and_binary(harness, tmp_path):
    missing_template = deploy.replace(harness.paths, template=tmp_path / "absent.plist")
    with pytest.raises(deploy.DeploymentError, match="template not found"):
        deploy.install(missing_template, harness.deps())

    harness.paths.binary.unlink()
    with pytest.raises(deploy.DeploymentError, match="missing or not executable"):
        harness.install()


def test_install_refuses_foreign_state_and_insecure_home(harness):
    harness.paths.default_lock.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.default_lock.write_text(json.dumps({"pid": 99}))
    harness.alive = {99}
    with pytest.raises(deploy.DeploymentError, match="foreign home"):
        harness.install()

    harness.paths.default_lock.unlink()
    harness.paths.default_home.chmod(0o777)
    with pytest.raises(deploy.DeploymentError, match="group/world writable"):
        harness.install()
    harness.paths.default_home.chmod(0o755)

    other_uid = harness.deps()
    other_uid.uid = os.getuid() + 1
    with pytest.raises(deploy.DeploymentError, match="not owned by uid"):
        deploy.install(harness.paths, other_uid)


def test_macos_gate_and_malformed_listen(harness):
    linux = deploy.replace(harness.deps(), platform="linux")
    with pytest.raises(deploy.DeploymentError, match="macOS-only"):
        deploy.install(harness.paths, linux)
    with pytest.raises(deploy.DeploymentError, match="malformed listen"):
        deploy._split_listen("not-a-listen-address")


def test_explicitly_pointing_zvec_grep_home_at_the_default_home_is_allowed(harness, monkeypatch):
    monkeypatch.setenv("ZVEC_GREP_HOME", str(harness.paths.default_home))

    assert harness.install() == 0


def test_backup_collision_gets_a_distinct_name(harness):
    harness.paths.plist.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.plist.write_text("deployed")
    deps = harness.deps()

    first = deploy._backup_plist(harness.paths, b"one", deps)
    second = deploy._backup_plist(harness.paths, b"two", deps)

    assert first is not None and second is not None and first != second


# ---------------------------------------------------------------------------
# CLI dispatch


def test_cli_dispatches_all_commands_and_reports_deployment_errors(harness, monkeypatch, capsys):
    _managed_install(harness)
    frozen = harness.deps()
    monkeypatch.setattr(deploy, "Deps", lambda *args, **kwargs: frozen)
    base = ["--home", str(harness.home), "--template", str(REPO_TEMPLATE)]

    assert deploy.main(base + ["verify"]) == 0
    assert deploy.main(base + ["uninstall"]) == 0
    assert deploy.main(base + ["install"]) == 0
    assert deploy.main(base + ["lock-repair"]) == 1
    assert "lock-repair requires" in capsys.readouterr().err


def test_conflicting_launchctl_primitives_are_treated_as_unverified(harness):
    class ConflictingPids(FakeLaunchd):
        def __call__(self, argv):
            arguments = list(argv[1:])
            if arguments == ["list", deploy.LABEL]:
                return deploy.CommandResult(0, '"PID" = 1;')
            if arguments == ["list"]:
                return deploy.CommandResult(0, f"2\t0\t{deploy.LABEL}\n")
            return super().__call__(argv)

    conflicting = deploy.replace(harness.deps(), run=ConflictingPids(harness.paths))
    with pytest.raises(deploy.DeploymentError, match="cannot verify launchd job state"):
        deploy.install(harness.paths, conflicting)


def test_uninstall_refuses_unverifiable_launchd_state(harness):
    _managed_install(harness)
    unreadable = deploy.replace(harness.deps(), run=lambda argv: deploy.CommandResult(1, "", "denied"))

    with pytest.raises(deploy.DeploymentError, match="cannot verify launchd state"):
        deploy.uninstall(harness.paths, unreadable)

    assert harness.paths.plist.exists()


def test_lock_repair_without_a_lock_is_a_noop_and_workspace_shapes(harness, capsys):
    harness.launchd.loaded = False
    assert deploy.lock_repair(harness.paths, harness.deps(), confirm=True) == 0
    assert "nothing to repair" in capsys.readouterr().out

    workspaces = harness.paths.default_home / "workspaces.json"
    workspaces.write_text('{"workspaces": [{"root": "/a"}, {"root": "/b"}]}')
    assert deploy._workspace_count(workspaces) == 2
    workspaces.write_text("not-json")
    assert deploy._workspace_count(workspaces) == 0


def test_plist_must_be_regular_and_writes_never_follow_symlinks(harness, tmp_path):
    harness.paths.plist.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.plist.mkdir()
    with pytest.raises(deploy.DeploymentError, match="not a regular file"):
        deploy._read_plist_bytes(harness.paths.plist)
    harness.paths.plist.rmdir()

    target = tmp_path / "atomic-target"
    target.write_text("keep")
    harness.paths.plist.symlink_to(target)
    with pytest.raises(deploy.DeploymentError, match="through a symlink"):
        deploy._atomic_write(harness.paths.plist, b"x", 0o644)
    assert target.read_text() == "keep"

    harness.paths.plist.unlink()
    assert deploy._backup_plist(harness.paths, b"x", harness.deps()) is None


def test_creating_a_home_under_a_foreign_parent_is_refused(harness):
    shutil.rmtree(harness.paths.default_home)
    foreign_uid = deploy.replace(harness.deps(), uid=os.getuid() + 1)

    with pytest.raises(deploy.DeploymentError, match="parent is not owned"):
        deploy.install(harness.paths, foreign_uid, allow_empty_home=True)


def test_rollback_restores_a_previous_render_and_stays_uninstallable(harness):
    _managed_install(harness)
    previous_manifest = deploy.load_manifest(harness.paths.manifest)
    assert previous_manifest is not None
    older = harness.paths.plist.read_bytes() + b"\n<!-- previous revision -->\n"
    deploy._atomic_write(harness.paths.plist, older, 0o644)
    deploy._write_manifest(
        harness.paths,
        deploy.replace(
            previous_manifest,
            render_sha256=deploy.sha256(older),
            bootstrapped_render_sha=deploy.sha256(older),
        ),
    )
    harness.launchd.fail_next_bootstraps(3)

    with pytest.raises(deploy.DeploymentError, match="Restored the previous plist"):
        harness.install(restart=True)

    assert harness.paths.plist.read_bytes() == older
    restored = deploy.load_manifest(harness.paths.manifest)
    assert restored is not None
    assert restored.render_sha256 == deploy.sha256(older)
    assert restored.bootstrapped_render_sha == deploy.sha256(older)
    assert deploy.uninstall(harness.paths, harness.deps()) == 0
    assert not harness.paths.plist.exists()


def test_bootout_failure_leaves_the_running_job_and_plist_untouched(harness):
    _managed_install(harness)
    current = harness.paths.plist.read_bytes()
    harness.launchd.failed_bootouts = 99

    with pytest.raises(deploy.DeploymentError, match="bootout failed"):
        harness.install(restart=True)

    assert harness.paths.plist.read_bytes() == current
    assert harness.launchd.loaded is True

    with pytest.raises(deploy.DeploymentError, match="bootout did not stop"):
        deploy.uninstall(harness.paths, harness.deps())
    assert harness.paths.plist.exists()


def test_verify_rejects_an_exit_zero_not_ready_status(harness):
    _managed_install(harness)
    harness.exec_result = deploy.CommandResult(0, "Server: not ready\n")

    with pytest.raises(deploy.DeploymentError, match="not ready under the default home"):
        harness.verify()


def test_install_refuses_an_unchanged_render_when_the_loaded_job_is_older(harness):
    _managed_install(harness)
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None
    deploy._write_manifest(harness.paths, deploy.replace(manifest, bootstrapped_render_sha=None))

    with pytest.raises(deploy.DeploymentError, match="older than the deployed plist"):
        harness.install()


def test_uninstall_stops_a_loaded_job_even_when_the_plist_is_missing(harness):
    _managed_install(harness)
    harness.paths.plist.unlink()

    assert deploy.uninstall(harness.paths, harness.deps()) == 0

    assert harness.launchd.bootout_calls()
    assert harness.launchd.loaded is False
    manifest = deploy.load_manifest(harness.paths.manifest)
    assert manifest is not None and manifest.uninstalled_at is not None


def test_lock_repair_preserves_evidence_and_repairs_a_malformed_lock(harness):
    harness.launchd.loaded = False
    harness.paths.default_lock.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.default_lock.write_text("not-json")

    assert deploy.lock_repair(harness.paths, harness.deps(), confirm=True) == 0
    preserved = list(harness.paths.default_lock.parent.glob("instance.lock.stale-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "not-json"

    harness.paths.default_lock.write_text(json.dumps({"pid": 888}))
    assert deploy.lock_repair(harness.paths, harness.deps(), confirm=True) == 0
    preserved = list(harness.paths.default_lock.parent.glob("instance.lock.stale-*"))
    assert len(preserved) == 2
    assert len({path.name for path in preserved}) == 2


def test_bootout_failure_after_adoption_leaves_no_bogus_manifest(harness):
    deployed = deploy.render_plist(REPO_TEMPLATE.read_text(), harness.paths)
    harness.paths.plist.parent.mkdir(parents=True, exist_ok=True)
    harness.paths.plist.write_bytes(deployed)
    harness.launchd.set_loaded(4321)
    harness.launchd.spawned.add(4321)
    harness.launchd.write_lock(4321)
    harness.launchd.failed_bootouts = 99

    with pytest.raises(deploy.DeploymentError, match="bootout failed"):
        harness.install(restart=True)

    assert harness.paths.plist.read_bytes() == deployed
    assert deploy.load_manifest(harness.paths.manifest) is None


def test_restored_manifest_carries_home_lifecycle_across_a_render_change():
    deps = deploy.Deps(platform="darwin", now=lambda: deploy.datetime(2026, 11, 26, tzinfo=deploy.timezone.utc))
    previous = deploy.Manifest(
        template_sha256="template",
        render_sha256="other-render",
        rendered_at="then",
        created_home=True,
    )

    restored = deploy._restored_manifest(previous, "restored-hash", deps)

    assert restored.render_sha256 == "restored-hash"
    assert restored.bootstrapped_render_sha == "restored-hash"
    assert restored.created_home is True
    assert restored.template_sha256 == "template"


def test_uninstall_cleans_a_created_home_when_the_plist_is_already_gone(harness):
    shutil.rmtree(harness.paths.default_home)
    assert harness.install(allow_empty_home=True) == 0
    harness.paths.plist.unlink()
    shutil.rmtree(harness.paths.default_home / "daemon")

    assert deploy.uninstall(harness.paths, harness.deps()) == 0
    assert not harness.paths.default_home.exists()
