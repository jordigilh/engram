#!/usr/bin/env python3
"""Per-workspace barrier broker for Serena invalidation git hooks.

The hook process is short-lived; this helper holds the exclusive flock across
the git transaction so a mutating Serena call (shared flock) cannot overlap a
checkout/merge rewrite. Deployment-owned; invoked by the generated hook.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import pathlib
import time


def _hold(args: argparse.Namespace) -> int:
    lock_path = pathlib.Path(args.lock)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + args.timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return 2
                time.sleep(0.05)
        pathlib.Path(args.ready).write_text(str(os.getpid()))
        release = pathlib.Path(args.release)
        end = time.monotonic() + args.max_hold
        while time.monotonic() < end:
            if release.exists():
                break
            time.sleep(0.1)
    finally:
        try:
            pathlib.Path(args.release).unlink()
        except OSError:
            pass
        try:
            pathlib.Path(args.ready).unlink()
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return 0


def _release(args: argparse.Namespace) -> int:
    pathlib.Path(args.release).touch()
    ready = pathlib.Path(args.ready)
    for _ in range(100):
        if not ready.exists():
            break
        time.sleep(0.02)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    hold = sub.add_parser("hold")
    hold.add_argument("--lock", required=True)
    hold.add_argument("--ready", required=True)
    hold.add_argument("--release", required=True)
    hold.add_argument("--timeout", type=float, default=5.0)
    hold.add_argument("--max-hold", type=float, default=60.0)
    release = sub.add_parser("release")
    release.add_argument("--ready", required=True)
    release.add_argument("--release", required=True)
    args = parser.parse_args()
    return _hold(args) if args.command == "hold" else _release(args)


if __name__ == "__main__":
    raise SystemExit(main())
