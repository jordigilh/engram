#!/usr/bin/env python3
"""Lazy per-workspace Serena process supervisor (issue #204).

One Serena process/endpoint per workspace is what makes invalidation surgical:
Serena's ``active_project`` is process-global, so separate transport sessions
inside one process are not enough. The supervisor spawns a deployment-provided
command (with ``{workspace}`` and ``{port}`` placeholders), waits for the
endpoint to listen, and evicts idle processes.

The command, ports, and idle policy are deployment configuration; this module
only owns process lifecycle.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import pathlib
import signal
import time
from dataclasses import dataclass

log = logging.getLogger("serena-multiplex.supervisor")


@dataclass
class _Process:
    process: asyncio.subprocess.Process
    url: str
    port: int
    last_used: float
    last_probe: float
    started_at: float


class WorkspaceProcessSupervisor:
    def __init__(
        self,
        command: list[str],
        *,
        port_base: int = 19000,
        port_span: int = 200,
        idle_ttl_s: float = 1800.0,
        ready_timeout_s: float = 30.0,
        liveness_timeout_s: float = 3.0,
        liveness_cache_s: float = 10.0,
        max_processes: int | None = None,
        clock=time.monotonic,
    ) -> None:
        if not command:
            raise ValueError("supervisor command must not be empty")
        self.command = command
        self.port_base = port_base
        self.port_span = port_span
        self.idle_ttl_s = idle_ttl_s
        self.ready_timeout_s = ready_timeout_s
        self.liveness_timeout_s = liveness_timeout_s
        self.liveness_cache_s = liveness_cache_s
        self.max_processes = max_processes
        self._clock = clock
        self._procs: dict[str, _Process] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._recycled: list[str] = []
        self._reserved_ports: set[int] = set()
        self._starting = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(workspace: str) -> str:
        return str(pathlib.Path(workspace).expanduser().resolve())

    def _render(self, workspace: str, port: int) -> list[str]:
        return [
            arg.replace("{workspace}", workspace).replace("{port}", str(port))
            for arg in self.command
        ]

    async def _next_port(self) -> int:
        used = {entry.port for entry in self._procs.values()} | self._reserved_ports
        for offset in range(self.port_span):
            port = self.port_base + offset
            if port not in used:
                self._reserved_ports.add(port)
                return port
        raise RuntimeError("no free supervisor ports")

    @staticmethod
    def _mcp_alive(port: int, timeout: float, require_success: bool = False) -> bool:
        """Request-level liveness. Startup requires 2xx; warm liveness accepts
        any HTTP answer as proof the process is responsive."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "serena-supervisor", "version": "0.0.1"},
                },
            }
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request(
                "POST",
                "/mcp",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            response = conn.getresponse()
            session_id = response.getheader("mcp-session-id")
            response.read()
            if session_id:
                try:
                    conn.request(
                        "DELETE", "/mcp", headers={"mcp-session-id": session_id}
                    )
                    conn.getresponse().read()
                except (OSError, http.client.HTTPException):
                    pass
            if require_success:
                return 200 <= response.status < 300
            return response.status < 600
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    async def _wait_ready(self, port: int) -> bool:
        deadline = self._clock() + self.ready_timeout_s
        while self._clock() < deadline:
            if await asyncio.to_thread(self._mcp_alive, port, 1.0, True):
                return True
            await asyncio.sleep(0.05)
        return False

    @staticmethod
    def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            process.terminate()

    async def _terminate_and_wait(self, process: asyncio.subprocess.Process) -> None:
        self._terminate(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    async def _stop_entry(self, entry: _Process) -> None:
        await self._terminate_and_wait(entry.process)

    async def _terminate_failed_start(
        self, process: asyncio.subprocess.Process, port: int | None
    ) -> None:
        """Reap a failed startup child, then release its port and slot.

        Runs as its own task so repeated cancellation of the caller cannot
        release the port while the child is still shutting down.
        """
        try:
            await self._terminate_and_wait(process)
        finally:
            async with self._lock:
                if port is not None:
                    self._reserved_ports.discard(port)
                self._starting -= 1

    async def _terminate_and_release(
        self, process: asyncio.subprocess.Process, port: int
    ) -> None:
        """Reap a retiring child, then release its port.

        Runs as its own task so cancelling the caller cannot free the port
        before the old listener has exited.
        """
        try:
            await self._terminate_and_wait(process)
        finally:
            async with self._lock:
                self._reserved_ports.discard(port)

    async def _retire_entry(self, key: str, entry: _Process, *, recycled: bool = True) -> None:
        async with self._lock:
            self._reserved_ports.add(entry.port)
        cleanup = asyncio.ensure_future(
            self._terminate_and_release(entry.process, entry.port)
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # The cleanup task owns the port and keeps running.
            raise
        if recycled:
            self._recycled.append(key)

    async def ensure(self, workspace: str) -> str:
        key = self._key(workspace)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            async with self._lock:
                entry = self._procs.get(key)
            if entry is not None and entry.process.returncode is None:
                now = self._clock()
                cold = now - entry.started_at < self.ready_timeout_s
                if not cold and now - entry.last_probe >= self.liveness_cache_s:
                    alive = await asyncio.to_thread(
                        self._mcp_alive, entry.port, self.liveness_timeout_s
                    )
                    entry.last_probe = now
                    if not alive:
                        log.warning("Serena for %s is hung; recycling", key)
                        async with self._lock:
                            self._procs.pop(key, None)
                        await self._retire_entry(key, entry)
                        entry = None
                if entry is not None:
                    entry.last_used = now
                    return entry.url

            async with self._lock:
                victim = None
                victim_key = None
                if (
                    self.max_processes is not None
                    and len(self._procs) + self._starting >= self.max_processes
                ):
                    candidates = list(self._procs.items())
                    if not candidates:
                        raise RuntimeError(
                            "all Serena process slots are starting; retry"
                        )
                    victim_key, victim = min(
                        candidates, key=lambda item: item[1].last_used
                    )
                    self._procs.pop(victim_key, None)
                self._starting += 1

            process = None
            reserved_port: int | None = None
            published = False
            cancelled = False
            try:
                if victim is not None:
                    await self._retire_entry(victim_key, victim)
                    log.info("evicted LRU Serena for %s", victim_key)

                async with self._lock:
                    port = await self._next_port()
                    reserved_port = port
                try:
                    process = await asyncio.create_subprocess_exec(
                        *self._render(key, port),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except OSError as exc:
                    raise RuntimeError(
                        f"could not spawn Serena for {key}: {exc}"
                    ) from exc

                ready = await self._wait_ready(port)
                if not ready:
                    raise RuntimeError(f"Serena did not listen on port {port}")

                url = f"http://127.0.0.1:{port}/mcp"
                async with self._lock:
                    self._reserved_ports.discard(port)
                    reserved_port = None
                    self._procs[key] = _Process(
                        process=process,
                        url=url,
                        port=port,
                        last_used=self._clock(),
                        last_probe=self._clock(),
                        started_at=self._clock(),
                    )
                    published = True
                log.info("started Serena for %s on %d", key, port)
                return url
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                if published:
                    self._starting -= 1
                elif process is not None:
                    cleanup = asyncio.ensure_future(
                        self._terminate_failed_start(process, reserved_port)
                    )
                    if not cancelled:
                        # Wait for a failed child to exit before propagating
                        # the startup error. On cancellation the cleanup task
                        # keeps running and owns the port and slot.
                        await asyncio.shield(cleanup)
                else:
                    if reserved_port is not None:
                        self._reserved_ports.discard(reserved_port)
                    self._starting -= 1

    async def drain_recycled(self) -> list[str]:
        async with self._lock:
            recycled = list(self._recycled)
            self._recycled.clear()
            return recycled

    async def recycle(self, workspace: str) -> bool:
        """Stop one workspace's process so the next ensure starts a fresh one."""
        key = self._key(workspace)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            async with self._lock:
                entry = self._procs.pop(key, None)
            if entry is None:
                return False
            await self._stop_entry(entry)
            self._recycled.append(key)
            return True

    async def endpoint(self, workspace: str) -> str | None:
        entry = self._procs.get(self._key(workspace))
        if entry is None or entry.process.returncode is not None:
            return None
        return entry.url

    async def evict_idle(self) -> list[str]:
        now = self._clock()
        async with self._lock:
            stale = [
                (key, entry)
                for key, entry in self._procs.items()
                if now - entry.last_used >= self.idle_ttl_s
            ]
            for key, _entry in stale:
                self._procs.pop(key, None)
        for key, entry in stale:
            await self._stop_entry(entry)
            log.info("evicted idle Serena for %s", key)
        return [key for key, _ in stale]

    async def stop(self) -> None:
        async with self._lock:
            entries = list(self._procs.values())
            self._procs.clear()
        for entry in entries:
            await self._stop_entry(entry)
