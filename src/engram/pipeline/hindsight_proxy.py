#!/usr/bin/env python3
"""Zero-downtime TCP proxy in front of hindsight-api.

Cursor holds a persistent HTTP connection to port 8888 for the hindsight /
hindsight-docs / hindsight-issues MCP servers. When hindsight-api itself is
killed and respawned (the nightly heap-reclaim restart -- see
docs/FINDINGS.md 2026-06-26), that connection drops, and Cursor's HTTP MCP
client does not auto-retry: the servers show disabled until a manual reload
(see docs/FINDINGS.md 2026-07-07 and 2026-08-02).

This proxy is the fix. It is the *only* thing that ever binds port 8888, and
it is never restarted by the nightly job. hindsight-api itself binds an
internal "blue" or "green" port instead (see hindsight-blue-green-restart.sh
and launchd/io.vectorize.hindsight.service-{blue,green}.plist). The proxy
re-reads which color is currently active from a state file on every new
client connection, so a blue/green swap is invisible to anything connected
to 8888 -- there is never a moment where 8888 refuses a connection.

Pure stdlib, no dependencies, so it can't be broken by a venv/package issue
independently of hindsight-api itself.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

LISTEN_HOST = os.environ.get("HINDSIGHT_PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("HINDSIGHT_PROXY_PORT", "8888"))
STATE_FILE = os.environ.get(
    "HINDSIGHT_ACTIVE_BACKEND_FILE",
    os.path.expanduser("~/.hindsight/state/active-backend.port"),
)
DEFAULT_BACKEND_PORT = int(os.environ.get("HINDSIGHT_DEFAULT_BACKEND_PORT", "18888"))
BACKEND_HOST = "127.0.0.1"
BACKEND_CONNECT_TIMEOUT_S = 5.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - hindsight-proxy - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("hindsight-proxy")


def read_active_backend_port() -> int:
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return DEFAULT_BACKEND_PORT


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def handle_client(
    client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
) -> None:
    peer = client_writer.get_extra_info("peername")
    backend_port = read_active_backend_port()
    try:
        backend_reader, backend_writer = await asyncio.wait_for(
            asyncio.open_connection(BACKEND_HOST, backend_port),
            timeout=BACKEND_CONNECT_TIMEOUT_S,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        log.error(
            "backend %s:%d unreachable for client %s: %s",
            BACKEND_HOST,
            backend_port,
            peer,
            exc,
        )
        client_writer.close()
        return

    await asyncio.gather(
        _pump(client_reader, backend_writer),
        _pump(backend_reader, client_writer),
    )


async def main() -> None:
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    log.info(
        "listening on %s:%d, backend state file=%s (default port %d)",
        LISTEN_HOST,
        LISTEN_PORT,
        STATE_FILE,
        DEFAULT_BACKEND_PORT,
    )
    async with server:
        await server.serve_forever()


def cli_main() -> None:
    """Sync entry point for the engram-hindsight-proxy console script --
    [project.scripts] wraps a plain callable, so async main() needs this
    asyncio.run() shim rather than being pointed at directly."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
