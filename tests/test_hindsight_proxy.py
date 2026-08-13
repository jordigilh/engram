"""Tests for hindsight_proxy.py -- the zero-downtime TCP proxy in front of
hindsight-api (see the module's own docstring for the blue/green rationale).

Business outcomes under test: the active-backend port is read dynamically
per-connection (so a blue/green swap is invisible to already-open client
connections at 8888), a missing/corrupt state file degrades to the
documented default rather than crashing, an unreachable backend is reported
and the client connection is cleanly closed rather than left hanging, and
byte-pumping between client and backend forwards data until EOF and
tolerates the peer disconnecting mid-stream. No real socket is ever bound in
this file -- that boundary (asyncio.start_server/serve_forever) is
integration-test territory, not unit-test territory.
"""
from __future__ import annotations

import asyncio


class TestReadActiveBackendPort:
    def test_reads_port_from_state_file(self, hindsight_proxy, tmp_path, monkeypatch):
        state_file = tmp_path / "active-backend.port"
        state_file.write_text("18889\n")
        monkeypatch.setattr(hindsight_proxy, "STATE_FILE", str(state_file))

        assert hindsight_proxy.read_active_backend_port() == 18889

    def test_missing_state_file_falls_back_to_default(self, hindsight_proxy, tmp_path, monkeypatch):
        monkeypatch.setattr(hindsight_proxy, "STATE_FILE", str(tmp_path / "does-not-exist.port"))
        monkeypatch.setattr(hindsight_proxy, "DEFAULT_BACKEND_PORT", 18888)

        assert hindsight_proxy.read_active_backend_port() == 18888

    def test_corrupt_state_file_content_falls_back_to_default(self, hindsight_proxy, tmp_path, monkeypatch):
        """A partially-written state file (e.g. caught mid-write during a
        blue/green swap) must not crash the proxy -- this is the whole
        reason the swap is supposed to be invisible to 8888."""
        state_file = tmp_path / "active-backend.port"
        state_file.write_text("not-a-port-number")
        monkeypatch.setattr(hindsight_proxy, "STATE_FILE", str(state_file))
        monkeypatch.setattr(hindsight_proxy, "DEFAULT_BACKEND_PORT", 18888)

        assert hindsight_proxy.read_active_backend_port() == 18888

    def test_surrounding_whitespace_is_stripped(self, hindsight_proxy, tmp_path, monkeypatch):
        state_file = tmp_path / "active-backend.port"
        state_file.write_text("  28888  \n")
        monkeypatch.setattr(hindsight_proxy, "STATE_FILE", str(state_file))

        assert hindsight_proxy.read_active_backend_port() == 28888


class _FakeStreamReader:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def read(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeStreamWriter:
    def __init__(self, extra_info=None, raise_on_write: Exception | None = None):
        self.written = bytearray()
        self.closed = False
        self._extra_info = extra_info or {}
        self._raise_on_write = raise_on_write

    def write(self, data):
        if self._raise_on_write:
            raise self._raise_on_write
        self.written.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def get_extra_info(self, key):
        return self._extra_info.get(key)


class TestPump:
    def test_forwards_all_chunks_until_eof_then_closes_writer(self, hindsight_proxy):
        reader = _FakeStreamReader([b"hello ", b"world"])
        writer = _FakeStreamWriter()

        asyncio.run(hindsight_proxy._pump(reader, writer))

        assert bytes(writer.written) == b"hello world"
        assert writer.closed is True

    def test_connection_reset_while_writing_is_swallowed_not_raised(self, hindsight_proxy):
        reader = _FakeStreamReader([b"data"])
        writer = _FakeStreamWriter(raise_on_write=ConnectionResetError())

        asyncio.run(hindsight_proxy._pump(reader, writer))  # must not raise

        assert writer.closed is True

    def test_broken_pipe_while_writing_is_swallowed_not_raised(self, hindsight_proxy):
        reader = _FakeStreamReader([b"data"])
        writer = _FakeStreamWriter(raise_on_write=BrokenPipeError())

        asyncio.run(hindsight_proxy._pump(reader, writer))  # must not raise

        assert writer.closed is True

    def test_immediate_eof_still_closes_writer(self, hindsight_proxy):
        reader = _FakeStreamReader([])
        writer = _FakeStreamWriter()

        asyncio.run(hindsight_proxy._pump(reader, writer))

        assert writer.written == b""
        assert writer.closed is True


class TestHandleClient:
    def test_unreachable_backend_closes_client_and_returns_without_raising(self, hindsight_proxy, monkeypatch):
        monkeypatch.setattr(hindsight_proxy, "read_active_backend_port", lambda: 18888)

        async def fake_open_connection(host, port):
            raise OSError("connection refused")

        monkeypatch.setattr(hindsight_proxy.asyncio, "open_connection", fake_open_connection)

        client_reader = _FakeStreamReader([])
        client_writer = _FakeStreamWriter(extra_info={"peername": ("127.0.0.1", 54321)})

        asyncio.run(hindsight_proxy.handle_client(client_reader, client_writer))  # must not raise

        assert client_writer.closed is True

    def test_backend_connect_timeout_closes_client_and_returns_without_raising(self, hindsight_proxy, monkeypatch):
        monkeypatch.setattr(hindsight_proxy, "read_active_backend_port", lambda: 18888)
        monkeypatch.setattr(hindsight_proxy, "BACKEND_CONNECT_TIMEOUT_S", 0.01)

        async def fake_open_connection(host, port):
            await asyncio.sleep(1)  # always slower than the 0.01s timeout above

        monkeypatch.setattr(hindsight_proxy.asyncio, "open_connection", fake_open_connection)

        client_reader = _FakeStreamReader([])
        client_writer = _FakeStreamWriter(extra_info={"peername": ("127.0.0.1", 54321)})

        asyncio.run(hindsight_proxy.handle_client(client_reader, client_writer))  # must not raise

        assert client_writer.closed is True

    def test_successful_connection_pumps_both_directions(self, hindsight_proxy, monkeypatch):
        monkeypatch.setattr(hindsight_proxy, "read_active_backend_port", lambda: 18888)

        backend_reader = _FakeStreamReader([b"from-backend"])
        backend_writer = _FakeStreamWriter()

        async def fake_open_connection(host, port):
            assert (host, port) == (hindsight_proxy.BACKEND_HOST, 18888)
            return backend_reader, backend_writer

        monkeypatch.setattr(hindsight_proxy.asyncio, "open_connection", fake_open_connection)

        client_reader = _FakeStreamReader([b"from-client"])
        client_writer = _FakeStreamWriter(extra_info={"peername": ("127.0.0.1", 54321)})

        asyncio.run(hindsight_proxy.handle_client(client_reader, client_writer))

        # client -> backend and backend -> client both actually forwarded.
        assert bytes(backend_writer.written) == b"from-client"
        assert bytes(client_writer.written) == b"from-backend"
