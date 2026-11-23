"""Supervisor coverage for per-workspace Serena processes (issue #204)."""
from __future__ import annotations

import asyncio
import socket
import sys

from engram.pipeline.serena_supervisor import WorkspaceProcessSupervisor

FAKE_SERVER = (
    "import http.server,sys\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_POST(self):\n"
    "        self.rfile.read(int(self.headers.get('Content-Length', 0)))\n"
    "        body = b'{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}'\n"
    "        self.send_response(200)\n"
    "        self.send_header('Content-Type', 'application/json')\n"
    "        self.send_header('Content-Length', str(len(body)))\n"
    "        self.end_headers()\n"
    "        self.wfile.write(body)\n"
    "    def log_message(self, *args):\n"
    "        pass\n"
    "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_spawns_one_process_and_reuses_it(tmp_path):
    base = _free_port()
    supervisor = WorkspaceProcessSupervisor(
        [sys.executable, "-c", FAKE_SERVER, "{port}"],
        port_base=base,
        port_span=5,
        ready_timeout_s=5,
    )

    async def run():
        try:
            first = await supervisor.ensure(str(tmp_path))
            second = await supervisor.ensure(str(tmp_path))
            assert first == second
            assert first.startswith(f"http://127.0.0.1:{base}/mcp")
            assert await supervisor.endpoint(str(tmp_path)) == first
        finally:
            await supervisor.stop()

    asyncio.run(run())


HANG_SERVER = (
    "import socket,sys,time;"
    "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1);"
    "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(1);"
    "c,_=s.accept(); time.sleep(30)"
)


def test_hung_listener_is_not_ready(tmp_path):
    import pytest

    supervisor = WorkspaceProcessSupervisor(
        [sys.executable, "-c", HANG_SERVER, "{port}"],
        port_base=_free_port(),
        port_span=5,
        ready_timeout_s=0.4,
    )

    async def run():
        try:
            with pytest.raises(RuntimeError, match="did not listen"):
                await supervisor.ensure(str(tmp_path))
        finally:
            await supervisor.stop()

    asyncio.run(run())


def test_concurrent_ensure_uses_distinct_ports(tmp_path):
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    supervisor = WorkspaceProcessSupervisor(
        [sys.executable, "-c", FAKE_SERVER, "{port}"],
        port_base=_free_port(),
        port_span=5,
        ready_timeout_s=5,
    )

    async def run():
        try:
            url_a, url_b = await asyncio.gather(
                supervisor.ensure(str(ws_a)), supervisor.ensure(str(ws_b))
            )
            assert url_a != url_b
        finally:
            await supervisor.stop()

    asyncio.run(run())


def test_max_processes_evicts_lru(tmp_path):
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    supervisor = WorkspaceProcessSupervisor(
        [sys.executable, "-c", FAKE_SERVER, "{port}"],
        port_base=_free_port(),
        port_span=5,
        ready_timeout_s=5,
        max_processes=1,
    )

    async def run():
        try:
            await supervisor.ensure(str(ws_a))
            await supervisor.ensure(str(ws_b))
            assert await supervisor.endpoint(str(ws_a)) is None
            assert await supervisor.endpoint(str(ws_b)) is not None
            recycled = await supervisor.drain_recycled()
            assert str(ws_a.resolve()) in recycled
        finally:
            await supervisor.stop()

    asyncio.run(run())


def test_recycle_stops_and_respawns(tmp_path):
    supervisor = WorkspaceProcessSupervisor(
        [sys.executable, "-c", FAKE_SERVER, "{port}"],
        port_base=_free_port(),
        port_span=5,
        ready_timeout_s=5,
    )

    async def run():
        try:
            first = await supervisor.ensure(str(tmp_path))
            assert await supervisor.recycle(str(tmp_path)) is True
            assert await supervisor.endpoint(str(tmp_path)) is None
            second = await supervisor.ensure(str(tmp_path))
            assert second.startswith("http://127.0.0.1:")
            assert first is not None
        finally:
            await supervisor.stop()

    asyncio.run(run())


def test_idle_eviction_stops_process(tmp_path):
    now = [100.0]
    supervisor = WorkspaceProcessSupervisor(
        [sys.executable, "-c", FAKE_SERVER, "{port}"],
        port_base=_free_port(),
        port_span=5,
        idle_ttl_s=10,
        ready_timeout_s=5,
        clock=lambda: now[0],
    )

    async def run():
        try:
            await supervisor.ensure(str(tmp_path))
            now[0] = 200.0
            await supervisor.evict_idle()
            assert await supervisor.endpoint(str(tmp_path)) is None
        finally:
            await supervisor.stop()

    asyncio.run(run())
