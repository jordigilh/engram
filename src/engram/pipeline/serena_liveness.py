#!/usr/bin/env python3
"""Request-level liveness watchdog for a Serena process (issue #204).

The 2026-11-21/22 hang class is a process-wide GIL/asyncio-dealloc stall: the
TCP listener stays open, no request is answered, SIGTERM may not be honoured,
and in-process ``restart_language_server`` cannot recover it. Only killing the
process works. This module probes the MCP endpoint with a bounded
``initialize`` request and runs a deployment-provided recovery command when the
probe fails.

The recovery command is deployment configuration (for example a ``pkill -9``
plus ``launchctl kickstart -k``); Engram ships only the mechanism.
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import subprocess
import time
from urllib.parse import urlsplit

log = logging.getLogger("serena-liveness")


def probe_mcp(url: str, timeout_s: float = 10.0) -> bool:
    """True only if the MCP endpoint answers initialize within the deadline."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"unsupported MCP URL: {url}")
    path = parts.path or "/mcp"
    conn = http.client.HTTPConnection(
        parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
        timeout=timeout_s,
    )
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "serena-liveness", "version": "0.0.1"},
            },
        }
    )
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        response = conn.getresponse()
        response.read()
        return 200 <= response.status < 300
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def recover(command: str) -> None:
    """Run the deployment-provided recovery command (SIGKILL/restart)."""
    log.warning("Serena liveness probe failed; running recovery command")
    try:
        subprocess.run(command, shell=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("recovery command failed: %s", exc)


def check_once(
    url: str,
    *,
    timeout_s: float = 10.0,
    on_hang: str | None = None,
) -> bool:
    healthy = probe_mcp(url, timeout_s)
    if healthy:
        log.info("Serena liveness OK: %s", url)
    elif on_hang:
        recover(on_hang)
    else:
        log.warning("Serena liveness probe failed (no recovery command): %s", url)
    return healthy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Serena MCP endpoint")
    parser.add_argument("--timeout-s", type=float, default=10.0)
    parser.add_argument("--interval-s", type=float, default=30.0)
    parser.add_argument(
        "--on-hang",
        help="Deployment command to SIGKILL/restart the hung process",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - serena-liveness - %(message)s",
    )
    while True:
        check_once(args.url, timeout_s=args.timeout_s, on_hang=args.on_hang)
        if args.once:
            return 0
        time.sleep(args.interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
