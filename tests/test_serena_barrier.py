"""Barrier broker coverage for the invalidation hooks (issue #204)."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "git-hooks/family-invalidate/serena_barrier.py"


def _wait_ready(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"barrier never became ready: {path}")


def test_hold_blocks_shared_lock_and_release_unblocks(tmp_path):
    lock = tmp_path / "ws.lock"
    ready = tmp_path / "ws.ready"
    release = tmp_path / "ws.release"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "hold",
            "--lock",
            str(lock),
            "--ready",
            str(ready),
            "--release",
            str(release),
            "--timeout",
            "1",
            "--max-hold",
            "10",
        ]
    )
    try:
        _wait_ready(ready)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(fd)

        subprocess.run(
            [sys.executable, str(SCRIPT), "release", "--ready", str(ready), "--release", str(release)],
            check=True,
            timeout=5,
        )
        assert proc.wait(timeout=5) == 0
        assert not ready.exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_hold_times_out_when_lock_is_held(tmp_path):
    lock = tmp_path / "ws.lock"
    ready = tmp_path / "ws.ready"
    release = tmp_path / "ws.release"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "hold",
                "--lock",
                str(lock),
                "--ready",
                str(ready),
                "--release",
                str(release),
                "--timeout",
                "0.2",
                "--max-hold",
                "1",
            ],
            timeout=5,
            check=False,
        )
        assert result.returncode == 2
        assert not ready.exists()
    finally:
        os.close(fd)
