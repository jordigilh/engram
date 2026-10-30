"""Regression coverage for the zvec container workspace bind mounts."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


WRAPPER = Path(__file__).parents[1] / "scripts" / "run-zvec-grep-container.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _run_wrapper(
    tmp_path: Path,
    *,
    temp_root: Path | None = None,
    generic_temp_root: Path | None = None,
    uname: str = "Linux",
    use_override: bool = True,
) -> tuple[subprocess.CompletedProcess[str], str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    runtime_log = tmp_path / "runtime.log"
    runtime = tmp_path / "fake-runtime"
    _write_executable(
        runtime,
        """#!/bin/sh
printf '%s\n' --- "$@" >> "$ZVEC_TEST_RUNTIME_LOG"
case "${1:-}" in
  inspect)
    if [ "${3:-}" = "{{.State.Running}}" ]; then
      printf '%s\n' false
    fi
    ;;
  pull|run|rm|logs) ;;
  *) exit 1 ;;
esac
""",
    )
    _write_executable(fake_bin / "uname", f"#!/bin/sh\nprintf '%s\\n' {uname}\n")
    if temp_root is not None:
        _write_executable(fake_bin / "getconf", f"#!/bin/sh\nprintf '%s\\n' {temp_root}\n")
    _write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")

    configured_root = tmp_path / "configured-root"
    configured_root.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "ZVEC_GREP_CONTAINER_RUNTIME": str(runtime),
            "ZVEC_GREP_STATE_DIR": str(tmp_path / "state"),
            "ZVEC_GREP_WORKSPACE_ROOTS": str(configured_root),
            "ZVEC_TEST_RUNTIME_LOG": str(runtime_log),
        }
    )
    if generic_temp_root is not None:
        env["TMPDIR"] = str(generic_temp_root)
    if temp_root is not None or not use_override:
        env.pop("ZVEC_GREP_OPENCODE_TEMP_ROOT", None)
    else:
        override = tmp_path / "override" / "opencode"
        override.mkdir(parents=True)
        env["ZVEC_GREP_OPENCODE_TEMP_ROOT"] = str(override)

    completed = subprocess.run(
        [str(WRAPPER)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return completed, runtime_log.read_text()


def _volume_values(runtime_log: str) -> set[str]:
    lines = runtime_log.splitlines()
    return {lines[index + 1] for index, line in enumerate(lines[:-1]) if line == "--volume"}


def test_auto_mounts_only_the_macos_opencode_temp_subtree(tmp_path):
    darwin_temp = tmp_path / "private" / "var" / "folders" / "user" / "T"
    darwin_temp.mkdir(parents=True)

    completed, runtime_log = _run_wrapper(tmp_path, temp_root=darwin_temp, uname="Darwin")

    # The fake monitor exits immediately after startup; mount construction is
    # complete before that expected harness-only shutdown.
    assert completed.returncode != 0
    mounted = _volume_values(runtime_log)
    configured = (tmp_path / "configured-root").resolve()
    opencode = (darwin_temp / "opencode").resolve()
    assert f"{configured}:{configured}:rw" in mounted
    assert f"{opencode}:{opencode}:rw" in mounted
    assert f"{darwin_temp}:{darwin_temp}:rw" not in mounted
    assert f"{tmp_path}:{tmp_path}:rw" not in mounted


def test_non_macos_does_not_auto_mount_shared_temp_directory(tmp_path):
    temp_base = tmp_path / "unix-temp"
    temp_base.mkdir()

    completed, runtime_log = _run_wrapper(
        tmp_path,
        generic_temp_root=temp_base,
        uname="Linux",
        use_override=False,
    )

    assert completed.returncode != 0
    mounted = _volume_values(runtime_log)
    opencode = (temp_base / "opencode").resolve()
    configured = (tmp_path / "configured-root").resolve()
    assert f"{configured}:{configured}:rw" in mounted
    assert f"{opencode}:{opencode}:rw" not in mounted
    assert f"{temp_base}:{temp_base}:rw" not in mounted


def test_explicit_override_is_narrow_and_does_not_replace_required_allowlist(tmp_path):
    completed, runtime_log = _run_wrapper(tmp_path, uname="Linux")

    assert completed.returncode != 0
    mounted = _volume_values(runtime_log)
    override = (tmp_path / "override" / "opencode").resolve()
    assert f"{override}:{override}:rw" in mounted
    assert f"{tmp_path}:{tmp_path}:rw" not in mounted

    env = os.environ.copy()
    env.update(
        {
            "ZVEC_GREP_CONTAINER_RUNTIME": str(tmp_path / "fake-runtime"),
            "ZVEC_GREP_STATE_DIR": str(tmp_path / "state-no-allowlist"),
            "ZVEC_GREP_OPENCODE_TEMP_ROOT": str(override),
        }
    )
    env.pop("ZVEC_GREP_WORKSPACE_ROOTS", None)
    rejected = subprocess.run(
        [str(WRAPPER)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert rejected.returncode != 0
    assert "ZVEC_GREP_WORKSPACE_ROOTS must explicitly authorize" in rejected.stderr
