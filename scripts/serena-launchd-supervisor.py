#!/usr/bin/env python3
"""Keep launchd in control of the actual Serena child process.

The launchd plists intentionally invoke this small, repository-owned wrapper
instead of relying on a shell or a package-manager shim. It forwards terminal
signals, preserves the child's exit status, and terminates the child during
shutdown so a replacement cannot be left behind holding the service port.
"""
from __future__ import annotations

import signal
import subprocess
import sys
from types import FrameType


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: serena-launchd-supervisor.py COMMAND [ARG ...]", file=sys.stderr)
        return 64

    child = subprocess.Popen(argv)

    def forward(signum: int, _frame: FrameType | None) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, forward)

    try:
        return child.wait()
    except KeyboardInterrupt:
        forward(signal.SIGTERM, None)
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
