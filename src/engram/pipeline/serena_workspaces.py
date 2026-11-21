#!/usr/bin/env python3
"""Per-workspace invalidation state for the Serena multiplex (issue #204).

This module owns the mechanism the multiplex needs to invalidate one workspace
without restarting the shared Serena process or disturbing other workspaces:

- a ``WorkspaceRuntime`` per canonical workspace path, with an epoch, a runtime
  generation, and a ``ready``/``flushing`` state;
- a FIFO-style admission gate per workspace (all calls serialized);
- an unthrottled worktree probe of the resolved HEAD object ID, the index, and
  the checked-out ref lock (leak-free, bounded);
- typed busy responses (``workspace_flushing``, ``workspace_unbound``,
  ``outcome_unknown``) instead of opaque timeouts/generic 503s;
- read-only retry safety: writes are never replayed.

The deployment owns process topology, tokens, and spool paths. This module only
implements the in-process state machine.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pathlib
import subprocess
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterator

log = logging.getLogger("serena-multiplex.workspaces")

PROBE_TIMEOUT_S = 1.0
CALL_QUEUE_TIMEOUT_S = 30.0
RETRY_AFTER_MS = 1000
DEDUP_WINDOW = 128

# Tools that are proven safe to retry after a transport/session failure. Any
# tool not listed is treated as a write (fail safe): never replayed.
READ_ONLY_TOOLS = frozenset(
    {
        "find_symbol",
        "find_referencing_symbols",
        "find_implementations",
        "get_symbols_overview",
        "get_current_config",
        "get_diagnostics_for_file",
        "search_for_pattern",
        "find_file",
        "list_dir",
        "read_file",
        "query_project",
    }
)

CONTROL_TOOLS = frozenset({"activate_project", "restart_language_server"})


def is_read_only_tool(name: str) -> bool:
    return name in READ_ONLY_TOOLS


def is_control_tool(name: str) -> bool:
    return name in CONTROL_TOOLS


class WorkspaceBusy(Exception):
    """A typed retryable/uncertain response for the multiplex to render."""

    def __init__(
        self,
        kind: str,
        workspace: str | None,
        *,
        epoch: int | None = None,
        retry_after_ms: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.workspace = workspace
        self.epoch = epoch
        self.retry_after_ms = retry_after_ms
        self.retryable = retryable


class WorkspaceAuthTimeout(Exception):
    """Authorization could not be decided because the git probe timed out."""


class WorkspaceBarrier:
    """Shared/exclusive flock between git hooks and mutating Serena calls.

    Git hooks hold the exclusive lock for the rewrite; writes hold a shared
    lock for the duration of the forwarded call. With no barrier configured the
    context manager is a no-op.
    """

    def __init__(self, path: str | None, workspace: str | None = None) -> None:
        self.path = path
        self.workspace = workspace

    def acquire_shared(self, timeout_s: float = 5.0) -> int:
        if not self.path:
            return -1
        path = pathlib.Path(self.path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise WorkspaceBusy(
                        "workspace_flushing",
                        self.workspace,
                        retry_after_ms=RETRY_AFTER_MS,
                    )
                time.sleep(0.05)

    @staticmethod
    def release_shared(fd: int) -> None:
        if fd < 0:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    @contextmanager
    def shared(self, timeout_s: float = 5.0) -> Iterator[None]:
        fd = self.acquire_shared(timeout_s)
        try:
            yield
        finally:
            self.release_shared(fd)


@dataclass
class ProbeResult:
    head: str | None
    fingerprint: str
    lock_held: bool = False
    error: str | None = None


@dataclass
class WorkspaceState:
    workspace: str
    epoch: int = 0
    activated_epoch: int = 0
    state: str = "flushing"  # start fail-closed until proven fresh
    runtime_generation: int = 0
    last_observed_head: str | None = None
    last_observed_fingerprint: str | None = None
    last_mutation_id: str | None = None
    seen_mutations: deque[str] = field(default_factory=lambda: deque(maxlen=DEDUP_WINDOW))
    git_active: set[str] = field(default_factory=set)
    terminal_transactions: deque[str] = field(
        default_factory=lambda: deque(maxlen=DEDUP_WINDOW)
    )


def _run_git(args: list[str], workspace: str, timeout_s: float) -> str | None:
    """Run git with a hard timeout; kill the whole process group on expiry."""
    try:
        proc = subprocess.Popen(
            ["git", "-C", workspace, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        if proc.returncode != 0:
            return None
        return out.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 15)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=0.25)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
            proc.communicate()
        log.warning("git probe timed out for %s %s", workspace, args)
        return None


def probe_worktree(workspace: str, timeout_s: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Probe the worktree only: HEAD OID, index, and the checked-out ref lock."""
    head = _run_git(["rev-parse", "HEAD"], workspace, timeout_s)
    if head is None:
        return ProbeResult(head=None, fingerprint="", error="git probe failed")

    git_dir = _run_git(["rev-parse", "--git-dir"], workspace, timeout_s)
    ref = _run_git(["symbolic-ref", "-q", "HEAD"], workspace, timeout_s)
    parts = [head]
    lock_held = False
    if git_dir:
        gd = pathlib.Path(git_dir)
        if not gd.is_absolute():
            gd = pathlib.Path(workspace) / gd
        for name in ("index.lock", "HEAD.lock"):
            if (gd / name).exists():
                lock_held = True
                parts.append(f"lock:{name}")
        if ref:
            # The ref itself may live in the common dir; the per-worktree lock
            # is what matters for this checkout.
            if (gd / (ref + ".lock")).exists():
                lock_held = True
                parts.append(f"lock:{ref}.lock")
    parts.append(f"ref:{ref or ''}")
    return ProbeResult(head=head, fingerprint="|".join(parts), lock_held=lock_held)


class WorkspaceRuntime:
    """One workspace's admission gate and epoch state."""

    def __init__(
        self,
        workspace: str,
        activate: Callable[[str], Awaitable[None]],
        restart_lsp: Callable[[str], Awaitable[None]],
        *,
        probe: Callable[[str], ProbeResult] = probe_worktree,
        settle_s: float = 0.0,
        activation_deadline_s: float = 20.0,
        queue_timeout_s: float = CALL_QUEUE_TIMEOUT_S,
        barrier_path: str | None = None,
        barrier_timeout_s: float = 5.0,
        state_path: str | None = None,
        backend_url: str | None = None,
        readiness_probe: Callable[[str], Awaitable[None]] | None = None,
        readiness_timeout_s: float = 30.0,
    ) -> None:
        self.workspace = workspace
        self.activate = activate
        self.restart_lsp = restart_lsp
        self.readiness_probe = readiness_probe
        self.readiness_timeout_s = readiness_timeout_s
        self.probe = probe
        self.settle_s = settle_s
        self.activation_deadline_s = activation_deadline_s
        self.queue_timeout_s = queue_timeout_s
        self.barrier = WorkspaceBarrier(barrier_path, self.workspace)
        self.barrier_timeout_s = barrier_timeout_s
        self.state_path = state_path
        self.backend_url = backend_url
        self.state = WorkspaceState(workspace=workspace)
        self._load_state()
        self._gate = asyncio.Lock()
        self._state_lock = asyncio.Lock()

    async def _locked_state(self) -> WorkspaceState:
        return self.state

    def _load_state(self) -> None:
        if not self.state_path:
            return
        try:
            data = json.loads(pathlib.Path(self.state_path).read_text())
        except (OSError, ValueError):
            return
        self.state.terminal_transactions = deque(
            data.get("terminal_transactions", []), maxlen=DEDUP_WINDOW
        )
        self.state.git_active = set(data.get("git_active", []))
        self.state.last_observed_head = data.get("last_observed_head")
        self.state.last_observed_fingerprint = data.get("last_observed_fingerprint")

    def _persist(self) -> None:
        if not self.state_path:
            return
        path = pathlib.Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "workspace": self.workspace,
            "terminal_transactions": list(self.state.terminal_transactions),
            "git_active": sorted(self.state.git_active),
            "last_observed_head": self.state.last_observed_head,
            "last_observed_fingerprint": self.state.last_observed_fingerprint,
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)

    async def apply_event(self, event: dict) -> dict:
        """Apply one hook event: begin, ref_committed, worktree_completed, aborted."""
        phase = event.get("phase")
        transaction_id = event.get("transaction_id")
        mutation_id = event.get("mutation_id")
        if phase not in {"begin", "ref_committed", "worktree_completed", "aborted"}:
            return {"applied": False, "reason": "unknown_phase"}
        async with self._state_lock:
            if (
                transaction_id
                and transaction_id in self.state.terminal_transactions
            ):
                return {"applied": False, "reason": "tombstoned"}
            if phase == "begin":
                if mutation_id is not None and mutation_id in self.state.seen_mutations:
                    return {"applied": False, "reason": "duplicate"}
                if mutation_id is not None:
                    self.state.seen_mutations.append(mutation_id)
                    self.state.last_mutation_id = mutation_id
                if transaction_id:
                    self.state.git_active.add(transaction_id)
            else:
                if transaction_id:
                    self.state.git_active.discard(transaction_id)
                    self.state.terminal_transactions.append(transaction_id)
            self.state.epoch += 1
            self.state.state = "flushing"
            self._persist()
            return {
                "applied": True,
                "epoch": self.state.epoch,
                "state": self.state.state,
                "transaction_id": transaction_id,
            }

    async def recover(
        self, transaction_id: str | None = None, *, force: bool = False
    ) -> list[str]:
        probe = await asyncio.to_thread(self.probe, self.workspace)
        if not force and (probe.lock_held or probe.error is not None):
            raise WorkspaceBusy(
                "workspace_flushing",
                self.workspace,
                epoch=self.state.epoch,
                retry_after_ms=RETRY_AFTER_MS,
            )
        async with self._state_lock:
            if transaction_id is None:
                cleared = sorted(self.state.git_active)
            else:
                cleared = [transaction_id]
            for item in cleared:
                self.state.git_active.discard(item)
                self.state.terminal_transactions.append(item)
            self.state.epoch += 1
            self.state.state = "flushing"
            self._persist()
            return cleared

    async def invalidate(self, mutation_id: str | None = None) -> bool:
        """Bump this workspace's epoch, surgically. Returns False on duplicate."""
        async with self._state_lock:
            if mutation_id is not None:
                if mutation_id in self.state.seen_mutations:
                    return False
                self.state.seen_mutations.append(mutation_id)
                self.state.last_mutation_id = mutation_id
            self.state.epoch += 1
            self.state.state = "flushing"
            return True

    async def runtime_lost(self) -> None:
        async with self._state_lock:
            self.state.runtime_generation += 1
            self.state.state = "flushing"
            self.state.activated_epoch = 0

    async def configure(
        self,
        activate: Callable[[str], Awaitable[None]],
        restart_lsp: Callable[[str], Awaitable[None]],
        *,
        settle_s: float | None = None,
        barrier_path: str | None = None,
        backend_url: str | None = None,
        readiness_probe: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Bind the current backend (per-workspace endpoint) to this runtime."""
        async with self._state_lock:
            if backend_url is not None and backend_url != self.backend_url:
                self.state.runtime_generation += 1
                self.state.state = "flushing"
                self.state.activated_epoch = 0
                self.backend_url = backend_url
            self.activate = activate
            self.restart_lsp = restart_lsp
            if readiness_probe is not None:
                self.readiness_probe = readiness_probe
            if settle_s is not None:
                self.settle_s = settle_s
        self.barrier = WorkspaceBarrier(barrier_path, self.workspace)

    async def _admit(self) -> tuple[int, int]:
        try:
            await asyncio.wait_for(self._gate.acquire(), timeout=self.queue_timeout_s)
        except asyncio.TimeoutError as exc:
            raise WorkspaceBusy(
                "workspace_flushing",
                self.workspace,
                epoch=self.state.epoch,
                retry_after_ms=RETRY_AFTER_MS,
            ) from exc
        return self.state.epoch, self.state.runtime_generation

    def _release(self) -> None:
        if self._gate.locked():
            self._gate.release()

    async def _apply_probe(self, probe: ProbeResult) -> tuple[int, int]:
        async with self._state_lock:
            if probe.error is not None:
                self.state.state = "flushing"
            elif probe.lock_held:
                self.state.state = "flushing"
            elif (
                self.state.last_observed_fingerprint is not None
                and probe.fingerprint != self.state.last_observed_fingerprint
            ):
                self.state.epoch += 1
                self.state.state = "flushing"
            self.state.last_observed_head = probe.head
            self.state.last_observed_fingerprint = probe.fingerprint
            return self.state.epoch, self.state.runtime_generation

    async def _ensure_ready(self, start_epoch: int, start_generation: int) -> ProbeResult:
        probe = await asyncio.to_thread(self.probe, self.workspace)
        if probe.lock_held or probe.error is not None:
            raise WorkspaceBusy(
                "workspace_flushing",
                self.workspace,
                epoch=start_epoch,
                retry_after_ms=RETRY_AFTER_MS,
            )

        async def _activate_and_restart() -> None:
            await self.activate(self.workspace)
            await self.restart_lsp(self.workspace)
            if self.settle_s:
                await asyncio.sleep(self.settle_s)

        try:
            await asyncio.wait_for(
                _activate_and_restart(), timeout=self.activation_deadline_s
            )
        except (asyncio.TimeoutError, Exception) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            log.warning("activation failed for %s: %s", self.workspace, exc)
            raise WorkspaceBusy(
                "workspace_flushing",
                self.workspace,
                epoch=start_epoch,
                retry_after_ms=RETRY_AFTER_MS,
            ) from exc

        settled = await asyncio.to_thread(self.probe, self.workspace)
        if settled.lock_held or settled.error is not None:
            raise WorkspaceBusy(
                "workspace_flushing",
                self.workspace,
                epoch=start_epoch,
                retry_after_ms=RETRY_AFTER_MS,
            )

        if self.readiness_probe is not None:
            try:
                await asyncio.wait_for(
                    self.readiness_probe(self.workspace),
                    timeout=self.readiness_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("readiness probe failed for %s: %s", self.workspace, exc)
                raise WorkspaceBusy(
                    "workspace_flushing",
                    self.workspace,
                    epoch=start_epoch,
                    retry_after_ms=RETRY_AFTER_MS,
                ) from exc

        async with self._state_lock:
            if (
                self.state.epoch != start_epoch
                or self.state.runtime_generation != start_generation
            ):
                self.state.state = "flushing"
                raise WorkspaceBusy(
                    "workspace_flushing",
                    self.workspace,
                    epoch=self.state.epoch,
                    retry_after_ms=RETRY_AFTER_MS,
                )
            self.state.state = "ready"
            self.state.activated_epoch = start_epoch
            return settled

    async def serve(
        self,
        tool_name: str,
        forward: Callable[[], Awaitable[object]],
    ) -> object:
        """Run one scoped call under the epoch fence.

        Returns whatever ``forward`` returns. Raises ``WorkspaceBusy`` for a
        typed retryable response or an uncertain write outcome.
        """
        start_epoch, start_generation = await self._admit()
        try:
            probe = await asyncio.to_thread(self.probe, self.workspace)
            start_epoch, start_generation = await self._apply_probe(probe)
            async with self._state_lock:
                needs_refresh = (
                    self.state.state != "ready"
                    or self.state.activated_epoch != start_epoch
                )
                current_epoch = self.state.epoch

            if needs_refresh:
                probe = await self._ensure_ready(current_epoch, start_generation)
                start_epoch = current_epoch

            if is_read_only_tool(tool_name):
                response = await forward()
            else:
                fd = await asyncio.to_thread(
                    self.barrier.acquire_shared, self.barrier_timeout_s
                )
                try:
                    async with self._state_lock:
                        ready = (
                            self.state.state == "ready"
                            and self.state.epoch == start_epoch
                            and self.state.activated_epoch == start_epoch
                            and self.state.runtime_generation == start_generation
                        )
                        if not ready:
                            self.state.state = "flushing"
                        epoch = self.state.epoch
                    if not ready:
                        raise WorkspaceBusy(
                            "workspace_flushing",
                            self.workspace,
                            epoch=epoch,
                            retry_after_ms=RETRY_AFTER_MS,
                        )
                    barrier_probe = await asyncio.to_thread(self.probe, self.workspace)
                    if (
                        barrier_probe.lock_held
                        or barrier_probe.error is not None
                        or barrier_probe.head != probe.head
                        or barrier_probe.fingerprint != probe.fingerprint
                    ):
                        raise WorkspaceBusy(
                            "workspace_flushing",
                            self.workspace,
                            epoch=epoch,
                            retry_after_ms=RETRY_AFTER_MS,
                        )
                    response = await forward()
                finally:
                    await asyncio.to_thread(
                        self.barrier.release_shared, fd
                    )

            post = await asyncio.to_thread(self.probe, self.workspace)
            async with self._state_lock:
                if post.error is not None or post.lock_held or (
                    post.head is not None
                    and probe.head is not None
                    and post.head != probe.head
                ):
                    self.state.epoch += 1
                    self.state.state = "flushing"
                still_fresh = (
                    self.state.epoch == start_epoch
                    and self.state.activated_epoch == start_epoch
                    and self.state.runtime_generation == start_generation
                    and self.state.state == "ready"
                )
                epoch = self.state.epoch
            if not still_fresh:
                if is_read_only_tool(tool_name):
                    raise WorkspaceBusy(
                        "workspace_flushing",
                        self.workspace,
                        epoch=epoch,
                        retry_after_ms=RETRY_AFTER_MS,
                    )
                raise WorkspaceBusy(
                    "outcome_unknown",
                    self.workspace,
                    epoch=epoch,
                    retry_after_ms=None,
                    retryable=False,
                )
            return response
        finally:
            self._release()


class WorkspaceRegistry:
    """Canonical workspace -> runtime. Single-owner by construction."""

    def __init__(self) -> None:
        self._runtimes: dict[str, WorkspaceRuntime] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def canonicalize(workspace: str) -> str:
        return str(pathlib.Path(workspace).expanduser().resolve())

    async def get_or_create(
        self,
        workspace: str,
        activate: Callable[[str], Awaitable[None]],
        restart_lsp: Callable[[str], Awaitable[None]],
        **kwargs,
    ) -> WorkspaceRuntime:
        key = self.canonicalize(workspace)
        async with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                runtime = WorkspaceRuntime(key, activate, restart_lsp, **kwargs)
                self._runtimes[key] = runtime
            return runtime

    async def get(self, workspace: str) -> WorkspaceRuntime | None:
        return self._runtimes.get(self.canonicalize(workspace))

    async def invalidate(self, workspace: str, mutation_id: str | None = None) -> bool:
        runtime = await self.get(workspace)
        if runtime is None:
            return False
        return await runtime.invalidate(mutation_id)

    async def __len__(self) -> int:
        return len(self._runtimes)


def typed_error_payload(
    message_id: object,
    kind: str,
    workspace: str | None,
    *,
    epoch: int | None = None,
    retry_after_ms: int | None = None,
    retryable: bool = True,
) -> dict:
    data: dict[str, object] = {"kind": kind, "retryable": retryable}
    if workspace is not None:
        data["workspace"] = workspace
    if epoch is not None:
        data["epoch"] = epoch
    if retry_after_ms is not None:
        data["retry_after_ms"] = retry_after_ms
    message = kind
    if workspace is not None:
        message = f"{kind}: {workspace}"
    if retry_after_ms is not None:
        message += f"; retry in {retry_after_ms // 1000 or 1}s"
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32002, "message": message, "data": data},
    }


def status_for_busy(busy: WorkspaceBusy) -> int:
    return 409 if busy.kind in {"workspace_unbound", "outcome_unknown"} else 503


def header_for_busy(busy: WorkspaceBusy) -> dict[str, str]:
    headers = {"X-Engram-Error": busy.kind, "Content-Type": "application/json"}
    if busy.kind in {"workspace_flushing", "workspace_auth_timeout"} and busy.retry_after_ms:
        headers["Retry-After"] = str(max(1, busy.retry_after_ms // 1000))
    return headers


def read_spool_events(
    spool_dir: str, workspace: str | None = None
) -> list[tuple[dict, pathlib.Path]]:
    """Read pending events without deleting them. Corrupt files are removed."""
    root = pathlib.Path(spool_dir).expanduser()
    if not root.is_dir():
        return []
    events: list[tuple[dict, pathlib.Path]] = []
    for path in sorted(root.glob("*.json")):
        try:
            event = json.loads(path.read_text())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if (
            not isinstance(event, dict)
            or "phase" not in event
            or "transaction_id" not in event
        ):
            # Not an event file (e.g. persisted state); leave it alone.
            continue
        if workspace is not None:
            try:
                same = (
                    WorkspaceRegistry.canonicalize(event.get("workspace", ""))
                    == workspace
                )
            except (OSError, RuntimeError):
                same = False
            if not same:
                continue
        events.append((event, path))
    return events


def drain_spool_dir(spool_dir: str, workspace: str | None = None) -> list[dict]:
    """Read and remove pending invalidation events. Best effort."""
    events = read_spool_events(spool_dir, workspace)
    for _, path in events:
        path.unlink(missing_ok=True)
    return [event for event, _ in events]


