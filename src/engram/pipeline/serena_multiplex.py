#!/usr/bin/env python3
"""HTTP wrapper that gives every kubernaut-family repo full read+write access
to the single shared Serena daemon (io.vectorize.serena.kubernaut-family),
instead of just whichever repo last called activate_project.

Background (see docs/findings/2026-08.md, 2026-08-13, eighth follow-up): the
shared Serena daemon was deliberately started *without* a fixed --project so
that activate_project stays available (Serena hard-disables that tool when a
project is pinned at startup). That unlocked full read+write for whichever
project is currently active -- but "currently active" is process-global
state, not per-MCP-session: verified empirically that a second, independent
session's activate_project call changes what an already-connected first
session's get_current_config reports. Two Cursor windows on two different
kubernaut-family repos therefore race on a shared upstream daemon; whichever
activate_project call lands last silently wins for *both* windows.

This module is the fix. It runs as its own tiny daemon in front of the real
Serena daemon and gives each repo a fixed mount path
(http://host:port/mcp/<project>). Every tool call arriving on a given mount
is preceded -- transparently, and serialized behind one process-wide lock --
by an activate_project(<that mount's own project>) call against the shared
upstream, so:
  - every repo gets full read+write (not just whichever one is "active"),
  - concurrent calls from different windows/repos are safely serialized
    instead of racing (the whole activate+call sequence is atomic), and
  - the caller never has to think about it -- explicit activate_project
    calls from the agent are intercepted rather than forwarded, since this
    mount's project is already fixed and correct.

Implementation note (2026-08-13, ninth follow-up spike): an earlier version
of this module used fastmcp's Client/create_proxy() to talk to the upstream
daemon. That client keeps a persistent SSE "GET" listen stream open with
automatic reconnect (standard MCP ClientSession behavior for receiving
server-initiated messages) -- and closing/reconnecting that stream raced
with Serena's own StreamableHTTPSessionManager, crashing the shared upstream
daemon during the spike (matches known, still-open upstream bugs: mcp SDK's
GET handler doesn't send a priming event before blocking, and cancelling a
mid-flight SSE task on client disconnect violates the ASGI response
lifecycle -- see PrefectHQ/fastmcp#532, #3025, #671). Plain POST-only HTTP
calls (proven safe in this same spike via curl, which never opens a GET at
all) don't hit this bug. This module therefore does NOT use fastmcp's
Client/create_proxy for the upstream connection. It hand-rolls a minimal POST
relay, keeps a wrapper-owned downstream session mapped to an upstream session,
and returns 405 for any GET against its own downstream-facing endpoint
(spec-compliant per the MCP Streamable HTTP transport spec: a server that
never pushes unsolicited messages may decline the SSE listen stream with 405
rather than opening one). The upstream session is recreated once if Serena
reports that it was lost, while the downstream session remains stable.

Not tied to "kubernaut-family" specifically -- `projects` and `upstream_url`
are both parameters, so any other Serena daemon shared across a repo family
could reuse this same module.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from engram.pipeline.serena_supervisor import WorkspaceProcessSupervisor
from engram.pipeline.serena_workspaces import (
    WorkspaceAuthTimeout,
    WorkspaceBusy,
    WorkspaceRegistry,
    header_for_busy,
    is_control_tool,
    is_read_only_tool,
    read_spool_events,
    status_for_busy,
    typed_error_payload,
)
from engram.pipeline.zvec_policy import authorized_zvec_root

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - serena-multiplex - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("serena-multiplex")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8893
DEFAULT_UPSTREAM_URL = "http://127.0.0.1:8892/mcp"
FORWARD_TIMEOUT_S = 60.0
RETRY_DELAY_S = 0.5
SESSION_IDLE_TIMEOUT_S = 30 * 60
ENGRAM_WORKSPACE_QUERY_PARAMETER = "workspace"

# Must match the `projects:` names registered in ~/.serena/serena_config.yml
# for the kubernaut-family daemon, not repo paths -- activate_project takes a
# project *name*.
KUBERNAUT_FAMILY_PROJECTS = [
    "kubernaut",
    "kubernaut-operator",
    "kubernaut-console",
    "kubernaut-demo-scenarios",
    "kubernaut-v1.5",
    "kubernaut-v1.6",
]

# Tools that operate on a specific, explicitly-named project passed in their
# own arguments (query_project, list_queryable_projects) rather than on
# "whatever is currently active" -- these never need an activate_project call
# first and are always safe to forward untouched. Everything else defaults
# to scoped (pinned before forwarding) so a future Serena tool this module
# doesn't know about yet fails safe rather than silently unprotected.
PROJECT_AGNOSTIC_TOOLS = frozenset({"query_project", "list_queryable_projects"})


def is_project_agnostic_tool(name: str) -> bool:
    return name in PROJECT_AGNOSTIC_TOOLS


def route_tool_call(tool_name: str) -> str:
    """Classify a tools/call target: "intercept" (internal control tools --
    redundant/dangerous to forward, this mount's project is already fixed),
    "agnostic" (safe to forward untouched, no activation needed), or
    "scoped" (default -- must pin this mount's project before forwarding)."""
    if is_control_tool(tool_name):
        return "intercept"
    if is_project_agnostic_tool(tool_name):
        return "agnostic"
    return "scoped"


class ActiveProjectTracker:
    """Tracks which project is currently active on the single shared
    upstream Serena daemon, and serializes activate+call sequences so two
    mounts can never interleave and land a real tool call in the wrong
    project. See module docstring for why this is necessary."""

    def __init__(self) -> None:
        self.active: str | None = None
        self._lock = None  # created lazily -- see lock property

    @property
    def lock(self):
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def pinned(self, project: str, activate: Callable[[str], Awaitable[None]]):
        """Guarantee `project` is active on the upstream for the duration of
        the `async with` block. Held for the caller's real tool call too, not
        just the activate step, so a concurrent pin for a different project
        can't sneak in between "we activated X" and "we actually used X"."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _cm():
            async with self.lock:
                if self.active != project:
                    await activate(project)
                    self.active = project
                yield

        return _cm()

    async def invalidate(self) -> None:
        """Force the next `pinned()` call to re-activate, even for the same
        project this tracker already believes is active.

        Needed because "active" here only reflects what *this multiplex*
        last told the upstream -- not the upstream's actual state. The
        shared upstream Serena daemon (io.vectorize.serena.kubernaut-family)
        gets force-restarted roughly every 10 minutes by the watch-mirror
        sync's reference-transaction hook (see docs/findings/2026-08.md,
        2026-08-20 entry), which silently wipes its real active-project
        state without this cache finding out. Call this once a forwarded
        response reveals that divergence (see
        `_looks_like_no_active_project_error`) so the next pin for the same
        project doesn't wrongly skip re-activation."""
        async with self.lock:
            self.active = None


@dataclass
class DownstreamSession:
    """The wrapper-owned identity and upstream session for one MCP client."""

    project: str
    upstream_session_id: str
    last_used: float
    workspace: str | None = None
    upstream_url: str | None = None


class SessionProjectMismatch(Exception):
    """Raised when a session is presented on a different project mount."""


class DownstreamSessionRegistry:
    """Maps opaque downstream MCP IDs to upstream Serena sessions.

    The registry is deliberately process-local. A multiplex restart invalidates
    all downstream sessions rather than persisting transport state that Serena
    can no longer honor.
    """

    def __init__(
        self,
        *,
        idle_timeout_s: float = SESSION_IDLE_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        import asyncio

        self.idle_timeout_s = idle_timeout_s
        self._clock = clock
        self._sessions: dict[str, DownstreamSession] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        project: str,
        upstream_session_id: str,
        workspace: str | None = None,
        upstream_url: str | None = None,
    ) -> tuple[str, DownstreamSession]:
        session_id = secrets.token_urlsafe(24)
        entry = DownstreamSession(
            project, upstream_session_id, self._clock(), workspace, upstream_url
        )
        async with self._lock:
            self._sessions[session_id] = entry
        return session_id, entry

    async def get(self, session_id: str, project: str) -> DownstreamSession | None:
        async with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            if entry.project != project:
                raise SessionProjectMismatch
            if self._clock() - entry.last_used >= self.idle_timeout_s:
                return None
            entry.last_used = self._clock()
            return entry

    async def replace_upstream(self, session_id: str, entry: DownstreamSession, upstream_session_id: str) -> None:
        async with self._lock:
            if self._sessions.get(session_id) is entry:
                entry.upstream_session_id = upstream_session_id
                entry.last_used = self._clock()

    async def bind_workspace(self, session_id: str, entry: DownstreamSession, workspace: str) -> bool:
        async with self._lock:
            if self._sessions.get(session_id) is not entry:
                return False
            if entry.workspace is not None:
                return entry.workspace == workspace
            entry.workspace = workspace
            entry.last_used = self._clock()
            return True

    async def remove(self, session_id: str, project: str) -> DownstreamSession | None:
        async with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            if entry.project != project:
                raise SessionProjectMismatch
            return self._sessions.pop(session_id)

    async def expire(self) -> list[DownstreamSession]:
        now = self._clock()
        async with self._lock:
            expired_ids = [
                session_id
                for session_id, entry in self._sessions.items()
                if now - entry.last_used >= self.idle_timeout_s
            ]
            return [self._sessions.pop(session_id) for session_id in expired_ids]

    async def __len__(self) -> int:
        async with self._lock:
            return len(self._sessions)


def _intercept_response(message: dict, project: str) -> dict:
    """Synthesize a well-formed JSON-RPC tool result for an agent's own
    (redundant) activate_project call, without contacting upstream at all."""
    text = (
        f"This mount is permanently pinned to project "
        f"'{project}' -- activate_project is managed "
        f"automatically and does not need to be called. This is a successful "
        f"no-op, not a routing failure. Do not use activate_project to switch "
        f"to another family repository through this mount; call Engram's "
        f"zvec_grep_search or zvec_search_code with the target root, or use "
        f"the target repository's own Engram mount for cross-repository "
        f"read-only access."
    )
    return {
        "jsonrpc": "2.0",
        "id": message.get("id"),
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": text,
                }
            ],
            # Serena's activate_project tool declares an output schema with a
            # required `result` string, so the synthetic result must satisfy it
            # just like the real upstream tool response does.
            "structuredContent": {"result": text},
            "isError": False,
        },
    }


def _authorize_workspace(
    raw_workspace: str | None,
    allowed_roots: frozenset[pathlib.Path],
) -> str | None:
    """Normalize and authorize a request's absolute workspace path.

    Exact configured roots and their registered top-level Git worktrees are
    accepted through the same fail-closed policy used by zvec. Nested paths,
    unrelated repositories, and non-Git roots are not accepted implicitly.
    """
    if raw_workspace is None:
        return None
    if not raw_workspace.strip() or not pathlib.Path(raw_workspace).is_absolute():
        raise ValueError("workspace must be an absolute authorized checkout path")
    authorized = authorized_zvec_root(raw_workspace, allowed_roots)
    if authorized is not None:
        return str(authorized)

    # Distinguish "authorization denied" from "the check itself timed out":
    # the latter must be a retryable typed error, not a misleading 403.
    import subprocess

    try:
        for args in (
            ["rev-parse", "--show-toplevel"],
            ["worktree", "list", "--porcelain"],
        ):
            subprocess.run(
                ["git", "-C", raw_workspace, *args],
                capture_output=True,
                check=False,
                timeout=2,
            )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceAuthTimeout(
            "workspace authorization check timed out"
        ) from exc
    except OSError:
        pass
    raise ValueError("workspace is outside the configured Serena root allowlist")


def _require_allowed_roots(allowed_roots: list[str] | None) -> list[str]:
    """Fail closed when the deployed multiplex has no workspace allowlist.

    The command-line daemon is the workspace-aware Serena deployment used by
    OpenCode/OpenChamber. Starting it without roots makes every current
    worktree unverifiable and makes an old/unscoped client look healthy while
    serving the canonical checkout. Fixed-workspace clients should use a
    direct stdio Serena backend instead.
    """
    if not allowed_roots:
        raise ValueError(
            "Serena multiplex requires at least one --allowed-root; "
            "configure canonical checkout roots before starting it"
        )
    if any(not isinstance(root, str) or not root.strip() for root in allowed_roots):
        raise ValueError(
            "Serena multiplex rejects blank --allowed-root values; "
            "configure canonical checkout roots before starting it"
        )
    return allowed_roots


async def handle_json_rpc_message(
    message: dict,
    project: str,
    tracker: ActiveProjectTracker,
    activate: Callable[[str], Awaitable[None]],
    forward: Callable[[dict], Awaitable[object]],
) -> object:
    """Core routing decision, independent of HTTP/ASGI wire format: decide
    whether `message` needs this mount's project pinned before forwarding,
    is safe to forward untouched, or should be answered locally without
    touching upstream. Returns whatever `forward`/the interceptor return."""
    if message.get("method") != "tools/call":
        return await forward(message)

    tool_name = (message.get("params") or {}).get("name", "")
    route = route_tool_call(tool_name)

    if route == "intercept":
        return _intercept_response(message, project)
    if route == "agnostic":
        return await forward(message)

    async with tracker.pinned(project, activate):
        return await forward(message)


def _parse_sse_json(body: bytes) -> dict:
    """Extract the JSON payload from a streamable-HTTP SSE-framed response
    (`event: message\\ndata: {...}\\n\\n`), or parse `body` as plain JSON if
    it isn't SSE-framed at all (Content-Type: application/json is also
    spec-legal for a single POST response)."""
    text = body.decode("utf-8", errors="replace").strip()
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    return json.loads(text)


def _looks_like_no_active_project_error(body: bytes) -> bool:
    """True if `body` is an MCP tool-call error response whose text reports
    Serena has no active project for this session -- the signature of a
    session that survived an upstream daemon restart while this multiplex's
    `ActiveProjectTracker` cache didn't get invalidated (see
    `ActiveProjectTracker.invalidate` and docs/findings/2026-08.md, 2026-08-20
    entry). Best-effort: any parse failure or unexpected shape is treated as
    "not this error" rather than raised, since `_forward_scoped` must not
    itself become a new failure mode on a malformed/unrelated response."""
    try:
        message = _parse_sse_json(body)
    except (ValueError, UnicodeDecodeError):
        return False

    result = message.get("result")
    if not isinstance(result, dict) or not result.get("isError"):
        return False

    content = result.get("content") or []
    text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    return "No active project" in text


def _looks_like_upstream_session_loss(status_code: int, body: bytes) -> bool:
    """Recognize the upstream responses that mean its session disappeared."""
    if status_code == 404:
        return True
    text = body.decode("utf-8", errors="replace").lower()
    return status_code == 400 and any(
        marker in text
        for marker in ("missing session id", "session not found", "invalid session")
    )


async def _forward_scoped(
    project: str,
    tracker: ActiveProjectTracker,
    activate: Callable[[str], Awaitable[None]],
    forward_raw: Callable[[], Awaitable[object]],
) -> object:
    """Pin `project` and forward, retrying exactly once (with a forced
    re-activation) if the response shows the upstream had no active project
    -- see `ActiveProjectTracker.invalidate` for why a cache hit here can
    still be stale. One retry only: if the upstream is genuinely down, a
    second identical failure is returned as-is rather than looping."""
    try:
        async with tracker.pinned(project, activate):
            response = await forward_raw()
    except Exception as exc:
        # Serena is periodically restarted by the mirror sync hook. If the
        # upstream disappears between activation and the real call, give
        # its supervisor one brief chance to bring it back instead of
        # turning the transport error into an opaque downstream 500.
        import httpx

        if not isinstance(exc, httpx.RequestError):
            raise
        await tracker.invalidate()
        import asyncio

        await asyncio.sleep(RETRY_DELAY_S)
        async with tracker.pinned(project, activate):
            return await forward_raw()

    if not _looks_like_no_active_project_error(getattr(response, "body", b"")):
        return response

    await tracker.invalidate()
    async with tracker.pinned(project, activate):
        return await forward_raw()


def _make_activate(
    upstream_url: str, upstream_host_header: str | None = None
) -> Callable[[str], Awaitable[None]]:
    """Build the `activate` callable for ActiveProjectTracker.pinned(): a
    short-lived, one-shot (POST-only, never GET) control session against the
    upstream daemon -- see module docstring for why GET/persistent-SSE
    clients are avoided here."""

    async def activate(project: str) -> None:
        await _control_call(
            upstream_url,
            upstream_host_header,
            "activate_project",
            {"project": project},
        )

    return activate


async def _control_call(
    upstream_url: str,
    upstream_host_header: str | None,
    tool_name: str,
    arguments: dict,
) -> None:
    """Run one one-shot control tool call through a short POST-only session."""
    import httpx

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if upstream_host_header:
        headers["Host"] = upstream_host_header
    async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT_S) as client:
        init_resp = await client.post(
            upstream_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "serena-multiplex-control", "version": "0.0.1"},
                },
            },
            headers=headers,
        )
        init_resp.raise_for_status()
        session_id = init_resp.headers.get("mcp-session-id")
        session_headers = {**headers, "mcp-session-id": session_id} if session_id else headers

        await client.post(
            upstream_url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )

        call_resp = await client.post(
            upstream_url,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            headers=session_headers,
        )
        call_resp.raise_for_status()
        result = _parse_sse_json(call_resp.content)
        if result.get("result", {}).get("isError"):
            raise RuntimeError(f"{tool_name} failed: {result}")

        if session_id:
            await client.delete(upstream_url, headers=session_headers)


def _make_restart_lsp(
    upstream_url: str, upstream_host_header: str | None = None
) -> Callable[[str], Awaitable[None]]:
    """Restart the workspace's language servers (not the Serena process)."""

    async def restart(_workspace: str) -> None:
        await _control_call(
            upstream_url,
            upstream_host_header,
            "restart_language_server",
            {},
        )

    return restart


def build_app(
    projects: list[str],
    upstream_url: str,
    upstream_host_header: str | None = None,
    *,
    allowed_roots: list[str | pathlib.Path] | None = None,
    invalidation_token: str | None = None,
    invalidation_spool_dir: str | None = None,
    require_workspace: str = "off",
    workspace_settle_s: float = 0.0,
    workspace_upstreams: dict[str, str] | None = None,
    workspace_barrier_dir: str | None = None,
    workspace_serena_command: list[str] | None = None,
    workspace_serena_idle_ttl_s: float = 1800.0,
    workspace_serena_port_base: int = 19000,
    allow_shared_invalidation: bool = False,
    lsp_readiness_symbol: str | None = None,
    lsp_readiness_file: str | None = None,
):
    """Build one POST-only, project-pinned route per configured project.

    ``workspace`` is optional for compatibility with existing fixed-project
    clients. When present, it is bound to the downstream MCP session and must
    be an exact configured root or a registered linked Git worktree of one.
    The authorized absolute path becomes Serena's activation target for every
    scoped call in that session.

    When invalidation is enabled (a token or spool directory is configured),
    scoped calls run under per-workspace epoch fencing and return typed
    ``workspace_flushing``/``outcome_unknown`` responses instead of opaque
    timeouts.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    tracker = ActiveProjectTracker()
    sessions = DownstreamSessionRegistry()
    activate = _make_activate(upstream_url, upstream_host_header)
    registry = WorkspaceRegistry()
    invalidation_enabled = bool(invalidation_token or invalidation_spool_dir)
    if require_workspace not in {"off", "warn", "enforce"}:
        raise ValueError("require_workspace must be off, warn, or enforce")
    workspace_upstreams = {
        WorkspaceRegistry.canonicalize(workspace): url
        for workspace, url in (workspace_upstreams or {}).items()
    }
    if len(set(workspace_upstreams.values())) != len(workspace_upstreams):
        raise ValueError("each workspace must map to a distinct Serena endpoint")
    if workspace_barrier_dir is None and invalidation_spool_dir:
        workspace_barrier_dir = str(
            pathlib.Path(invalidation_spool_dir).expanduser() / ".barrier"
        )
    supervisor = (
        WorkspaceProcessSupervisor(
            workspace_serena_command,
            port_base=workspace_serena_port_base,
            idle_ttl_s=workspace_serena_idle_ttl_s,
        )
        if workspace_serena_command
        else None
    )
    if invalidation_enabled and not invalidation_token:
        raise ValueError("invalidation requires a token")
    if invalidation_enabled and not (lsp_readiness_symbol or lsp_readiness_file):
        raise ValueError(
            "invalidation requires a revision-correlated readiness probe "
            "(--lsp-readiness-symbol or --lsp-readiness-file)"
        )
    if (
        invalidation_enabled
        and supervisor is None
        and not workspace_upstreams
        and not allow_shared_invalidation
    ):
        raise ValueError(
            "invalidation requires per-workspace endpoints "
            "(--workspace-upstream or --workspace-serena-command); pass "
            "allow_shared_invalidation only for migration"
        )
    authorized_roots = frozenset(
        pathlib.Path(root).expanduser().resolve()
        for root in (allowed_roots or [])
    )

    async def _runtime_for(workspace: str, *, allow_spawn: bool = True):
        key = WorkspaceRegistry.canonicalize(workspace)
        if allow_spawn:
            url = await _url_for_workspace(key)
        else:
            url = workspace_upstreams.get(key, upstream_url)

        async def readiness_probe(_workspace: str) -> None:
            if lsp_readiness_symbol:
                await _control_call(
                    url,
                    upstream_host_header,
                    "find_symbol",
                    {"name_path_pattern": lsp_readiness_symbol, "depth": 0},
                )
            elif lsp_readiness_file:
                await _control_call(
                    url,
                    upstream_host_header,
                    "get_symbols_overview",
                    {"relative_path": lsp_readiness_file},
                )
            else:  # pragma: no cover - build_app rejects this configuration
                raise RuntimeError("no revision-correlated readiness probe configured")

        barrier_path = None
        state_path = None
        if workspace_barrier_dir:
            import hashlib

            digest = hashlib.sha256(key.encode()).hexdigest()[:16]
            barrier_path = str(
                pathlib.Path(workspace_barrier_dir).expanduser() / f"{digest}.lock"
            )
        if invalidation_spool_dir:
            import hashlib

            digest = hashlib.sha256(key.encode()).hexdigest()[:16]
            state_path = str(
                pathlib.Path(invalidation_spool_dir).expanduser()
                / ".state"
                / f"{digest}.state.json"
            )
        runtime = await registry.get_or_create(
            workspace,
            _make_activate(url, upstream_host_header),
            _make_restart_lsp(url, upstream_host_header),
            settle_s=workspace_settle_s,
            barrier_path=barrier_path,
            state_path=state_path,
            backend_url=url,
            readiness_probe=readiness_probe,
        )
        if allow_spawn:
            await runtime.configure(
                _make_activate(url, upstream_host_header),
                _make_restart_lsp(url, upstream_host_header),
                settle_s=workspace_settle_s,
                barrier_path=barrier_path,
                backend_url=url,
                readiness_probe=readiness_probe,
            )
        return runtime

    def _busy_response(busy: WorkspaceBusy, message: dict | None) -> Response:
        message_id = (message or {}).get("id")
        if message is None or "id" not in (message or {}):
            return Response(
                content=b"",
                status_code=status_for_busy(busy),
                headers=header_for_busy(busy),
            )
        payload = typed_error_payload(
            message_id,
            busy.kind,
            busy.workspace,
            epoch=busy.epoch,
            retry_after_ms=busy.retry_after_ms,
            retryable=busy.retryable,
        )
        return Response(
            content=json.dumps(payload).encode(),
            status_code=status_for_busy(busy),
            headers=header_for_busy(busy),
            media_type="application/json",
        )

    # Headers that are connection/host-specific and must not be blindly
    # relayed between the two hops of this proxy.
    _DROP_REQUEST_HEADERS = {"host", "content-length", "connection"}
    _DROP_RESPONSE_HEADERS = {"content-length", "connection", "transfer-encoding"}

    async def _upstream_request(
        url: str, method: str, body: bytes, headers: dict[str, str]
    ) -> Response:
        import httpx

        async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT_S) as client:
            upstream_resp = await client.request(method, url, content=body, headers=headers)
        resp_headers = {
            k: v
            for k, v in upstream_resp.headers.items()
            if k.lower() not in _DROP_RESPONSE_HEADERS
        }
        return Response(content=upstream_resp.content, status_code=upstream_resp.status_code, headers=resp_headers)

    async def _url_for_workspace(workspace: str | None) -> str:
        if workspace is None:
            return upstream_url
        key = WorkspaceRegistry.canonicalize(workspace)
        if key in workspace_upstreams:
            return workspace_upstreams[key]
        if supervisor is not None:
            try:
                url = await supervisor.ensure(key)
            except RuntimeError as exc:
                log.warning("could not start Serena for %s: %s", key, exc)
                raise WorkspaceBusy(
                    "workspace_flushing", key, retry_after_ms=1000
                ) from exc
            for recycled in await supervisor.drain_recycled():
                runtime = await registry.get(recycled)
                if runtime is not None:
                    await runtime.runtime_lost()
            return url
        return upstream_url

    def _upstream_headers(headers: dict[str, str]) -> dict[str, str]:
        if upstream_host_header:
            headers = {**headers, "Host": upstream_host_header}
        return headers

    async def _open_upstream_session(url: str) -> tuple[str, Response]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        init_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "serena-multiplex", "version": "0.0.1"},
                },
            }
        ).encode()
        init_response = await _upstream_request(
            url, "POST", init_body, _upstream_headers(headers)
        )
        if init_response.status_code >= 400:
            raise RuntimeError(f"Serena initialize failed with HTTP {init_response.status_code}")
        upstream_session_id = next(
            (
                value
                for key, value in init_response.headers.items()
                if key.lower() == "mcp-session-id"
            ),
            None,
        )
        if not upstream_session_id:
            raise RuntimeError("Serena upstream did not return mcp-session-id")

        session_headers = {**headers, "mcp-session-id": upstream_session_id}
        initialized_body = b'{"jsonrpc":"2.0","method":"notifications/initialized"}'
        initialized_response = await _upstream_request(
            url, "POST", initialized_body, _upstream_headers(session_headers)
        )
        if initialized_response.status_code >= 400:
            raise RuntimeError(f"Serena initialized notification failed with HTTP {initialized_response.status_code}")
        return upstream_session_id, init_response

    async def _close_upstream_session(url: str, upstream_session_id: str) -> None:
        try:
            await _upstream_request(
                url,
                "DELETE",
                b"",
                _upstream_headers({"mcp-session-id": upstream_session_id}),
            )
        except Exception as exc:
            log.info("could not close upstream Serena session: %s", exc)

    def _with_downstream_session(response: Response, session_id: str) -> Response:
        response.headers["mcp-session-id"] = session_id
        return response

    def _make_endpoint(project: str):
        async def endpoint(request: Request) -> Response:
            if request.method == "GET":
                # Spec-compliant decline of the SSE listen stream -- see
                # module docstring for why this proxy never opens one.
                return Response(status_code=405)

            if request.method not in {"POST", "DELETE"}:
                return Response(status_code=405)

            raw_workspace = request.query_params.get(ENGRAM_WORKSPACE_QUERY_PARAMETER)
            try:
                requested_workspace = await asyncio.to_thread(
                    _authorize_workspace, raw_workspace, authorized_roots
                )
            except WorkspaceAuthTimeout:
                return Response(
                    content=json.dumps(
                        {
                            "detail": "workspace authorization check timed out; retry"
                        }
                    ).encode(),
                    status_code=503,
                    headers={
                        "X-Engram-Error": "workspace_auth_timeout",
                        "Retry-After": "1",
                    },
                    media_type="application/json",
                )
            except ValueError as exc:
                return Response(status_code=403, content=str(exc).encode())

            expired = await sessions.expire()
            for entry in expired:
                await _close_upstream_session(
                    entry.upstream_url or upstream_url, entry.upstream_session_id
                )
            if supervisor is not None:
                for evicted in await supervisor.evict_idle():
                    runtime = await registry.get(evicted)
                    if runtime is not None:
                        await runtime.runtime_lost()

            downstream_session_id = request.headers.get("mcp-session-id")
            if request.method == "DELETE":
                if not downstream_session_id:
                    return Response(status_code=400, content=b"Missing mcp-session-id")
                try:
                    entry = await sessions.remove(downstream_session_id, project)
                except SessionProjectMismatch:
                    return Response(status_code=404)
                if entry is None:
                    return Response(status_code=404)
                await _close_upstream_session(
                    entry.upstream_url or upstream_url, entry.upstream_session_id
                )
                return Response(status_code=200)

            body = await request.body()
            try:
                message = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return Response(status_code=400, content=b"Invalid JSON")

            if message.get("method") == "initialize":
                if (
                    invalidation_enabled
                    and require_workspace == "enforce"
                    and requested_workspace is None
                ):
                    return _busy_response(
                        WorkspaceBusy("workspace_unbound", None), message
                    )
                session_url = await _url_for_workspace(requested_workspace)
                try:
                    upstream_session_id, response = await _open_upstream_session(session_url)
                    downstream_session_id, _ = await sessions.create(
                        project,
                        upstream_session_id,
                        requested_workspace,
                        upstream_url=session_url,
                    )
                except WorkspaceBusy as busy:
                    return _busy_response(busy, message)
                except Exception as exc:
                    log.warning("could not initialize Serena session for %s: %s", project, exc)
                    return Response(
                        content=json.dumps({"detail": "Serena upstream is temporarily unavailable"}),
                        status_code=503,
                        headers={"Retry-After": "1"},
                        media_type="application/json",
                    )
                return _with_downstream_session(response, downstream_session_id)

            if not downstream_session_id:
                return Response(status_code=400, content=b"Missing mcp-session-id")
            try:
                entry = await sessions.get(downstream_session_id, project)
            except SessionProjectMismatch:
                return Response(status_code=404)
            if entry is None:
                return Response(status_code=404, content=b"Unknown or expired mcp-session-id")

            if requested_workspace is not None:
                if not await sessions.bind_workspace(downstream_session_id, entry, requested_workspace):
                    return Response(status_code=409, content=b"MCP session is bound to another workspace")

            async def forward_raw() -> Response:
                headers = {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() not in _DROP_REQUEST_HEADERS and k.lower() != "mcp-session-id"
                }
                headers["mcp-session-id"] = entry.upstream_session_id
                url = await _url_for_workspace(entry.workspace)
                return await _upstream_request(
                    url, "POST", body, _upstream_headers(headers)
                )

            async def refresh_session() -> None:
                session_url = await _url_for_workspace(entry.workspace)
                upstream_session_id, _ = await _open_upstream_session(session_url)
                await sessions.replace_upstream(downstream_session_id, entry, upstream_session_id)

            async def forward_with_recovery() -> Response:
                tool_name = (message.get("params") or {}).get("name", "")
                route = route_tool_call(tool_name) if message.get("method") == "tools/call" else "agnostic"

                if route == "intercept":
                    result = _intercept_response(message, project)
                    return Response(
                        content=f"event: message\ndata: {json.dumps(result)}\n\n",
                        media_type="text/event-stream",
                    )

                if invalidation_enabled:
                    effective_workspace = entry.workspace or requested_workspace
                    if effective_workspace is None and require_workspace == "enforce":
                        return _busy_response(
                            WorkspaceBusy("workspace_unbound", None), message
                        )
                    if route == "agnostic":
                        return _busy_response(
                            WorkspaceBusy("workspace_unbound", effective_workspace),
                            message,
                        )
                    if effective_workspace is not None:
                        runtime = await _runtime_for(effective_workspace)
                        if invalidation_spool_dir:
                            for event, path in read_spool_events(
                                invalidation_spool_dir, runtime.workspace
                            ):
                                await runtime.apply_event(event)
                                path.unlink(missing_ok=True)

                        async def forward_once() -> Response:
                            response = await forward_raw()
                            if is_read_only_tool(tool_name) and _looks_like_upstream_session_loss(
                                response.status_code, response.body
                            ):
                                await refresh_session()
                                response = await forward_raw()
                            return response

                        try:
                            return await runtime.serve(tool_name, forward_once)
                        except WorkspaceBusy as busy:
                            return _busy_response(busy, message)

                if route == "scoped":
                    response = await _forward_scoped(
                        entry.workspace or project,
                        tracker,
                        activate,
                        forward_raw,
                    )
                else:
                    response = await forward_raw()
                if not _looks_like_upstream_session_loss(response.status_code, response.body):
                    return response

                await refresh_session()
                await tracker.invalidate()
                if route == "scoped":
                    return await _forward_scoped(
                        entry.workspace or project,
                        tracker,
                        activate,
                        forward_raw,
                    )
                return await forward_raw()

            try:
                return _with_downstream_session(await forward_with_recovery(), downstream_session_id)
            except Exception as exc:
                import httpx

                if not isinstance(exc, httpx.RequestError):
                    raise
                log.warning("upstream Serena unavailable for project %s: %s", project, exc)
                return Response(
                    content=json.dumps({"detail": "Serena upstream is temporarily unavailable"}),
                    status_code=503,
                    headers={"Retry-After": "1"},
                    media_type="application/json",
                )

        return endpoint

    async def invalidate_endpoint(request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405)
        if invalidation_token:
            provided = request.headers.get("X-Engram-Invalidation-Token") or ""
            if not secrets.compare_digest(provided, invalidation_token):
                return Response(status_code=403, content=b"Invalid invalidation token")
        try:
            message = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return Response(status_code=400, content=b"Invalid JSON")
        try:
            workspace = await asyncio.to_thread(
                _authorize_workspace, message.get("workspace"), authorized_roots
            )
        except WorkspaceAuthTimeout:
            return _busy_response(
                WorkspaceBusy(
                    "workspace_auth_timeout",
                    message.get("workspace"),
                    retry_after_ms=1000,
                ),
                message,
            )
        except ValueError as exc:
            return Response(status_code=403, content=str(exc).encode())
        if workspace is None:
            return Response(status_code=400, content=b"workspace is required")
        runtime = await _runtime_for(workspace, allow_spawn=False)
        result = await runtime.apply_event(message)
        return Response(
            content=json.dumps(
                {
                    "workspace": runtime.workspace,
                    "epoch": runtime.state.epoch,
                    "state": runtime.state.state,
                    "applied": result.get("applied"),
                    "reason": result.get("reason"),
                }
            ).encode(),
            status_code=200,
            media_type="application/json",
        )

    async def recover_endpoint(request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405)
        if invalidation_token:
            provided = request.headers.get("X-Engram-Invalidation-Token") or ""
            if not secrets.compare_digest(provided, invalidation_token):
                return Response(status_code=403, content=b"Invalid invalidation token")
        try:
            message = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return Response(status_code=400, content=b"Invalid JSON")
        if not message.get("reason"):
            return Response(status_code=400, content=b"reason is required")
        try:
            workspace = await asyncio.to_thread(
                _authorize_workspace, message.get("workspace"), authorized_roots
            )
        except WorkspaceAuthTimeout:
            return _busy_response(
                WorkspaceBusy(
                    "workspace_auth_timeout",
                    message.get("workspace"),
                    retry_after_ms=1000,
                ),
                message,
            )
        except ValueError as exc:
            return Response(status_code=403, content=str(exc).encode())
        if workspace is None:
            return Response(status_code=400, content=b"workspace is required")
        runtime = await _runtime_for(workspace, allow_spawn=False)
        try:
            cleared = await runtime.recover(
                message.get("transaction_id"),
                force=bool(message.get("force")),
            )
        except WorkspaceBusy as busy:
            return _busy_response(busy, message)
        log.info(
            "serena recovery workspace=%s transactions=%s force=%s reason=%s",
            runtime.workspace,
            cleared,
            bool(message.get("force")),
            message.get("reason"),
        )
        return Response(
            content=json.dumps(
                {"workspace": runtime.workspace, "cleared": cleared}
            ).encode(),
            status_code=200,
            media_type="application/json",
        )

    routes = [
        Route(f"/mcp/{project}", _make_endpoint(project), methods=["GET", "POST", "DELETE"])
        for project in projects
    ]
    if invalidation_enabled:
        routes.append(
            Route("/admin/invalidate", invalidate_endpoint, methods=["POST"])
        )
        routes.append(Route("/admin/recover", recover_endpoint, methods=["POST"]))
    return Starlette(routes=routes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Explicitly allow binding the unauthenticated /mcp routes off loopback.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--upstream-url", default=DEFAULT_UPSTREAM_URL)
    parser.add_argument(
        "--upstream-host-header",
        help="Optional Host header for an upstream reached through a mapped address",
    )
    parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        help="Repeatable. Defaults to the kubernaut-family project list.",
    )
    parser.add_argument(
        "--allowed-root",
        dest="allowed_roots",
        action="append",
        help="Required and repeatable exact checkout root; registered linked Git worktrees are accepted too.",
    )
    parser.add_argument(
        "--invalidation-token-file",
        help="File containing the shared token required on /admin/invalidate.",
    )
    parser.add_argument(
        "--invalidation-spool-dir",
        help="Durable directory of git-hook invalidation events to drain.",
    )
    parser.add_argument(
        "--require-workspace",
        choices=["off", "warn", "enforce"],
        default=None,
        help="Binding policy. Defaults to enforce when invalidation is enabled, off otherwise.",
    )
    parser.add_argument(
        "--workspace-settle-s",
        type=float,
        default=0.0,
        help="Stability window required before a restarted workspace is ready.",
    )
    parser.add_argument(
        "--workspace-upstream",
        action="append",
        dest="workspace_upstreams",
        help="Repeatable <workspace-path>=<serena-endpoint-url> mapping; each "
        "workspace must use a distinct endpoint.",
    )
    parser.add_argument(
        "--workspace-barrier-dir",
        help="Directory of per-workspace barrier lock files shared with the "
        "git hooks.",
    )
    parser.add_argument(
        "--workspace-serena-command",
        help="Command template to spawn one Serena per workspace; use "
        "{workspace} and {port} placeholders. Example: "
        "'serena start-mcp-server --transport streamable-http --port {port} "
        "--project {workspace}'.",
    )
    parser.add_argument(
        "--workspace-serena-idle-ttl-s",
        type=float,
        default=1800.0,
        help="Idle seconds before a per-workspace Serena process is evicted.",
    )
    parser.add_argument(
        "--workspace-serena-port-base",
        type=int,
        default=19000,
        help="First port considered when spawning per-workspace Serena processes.",
    )
    parser.add_argument(
        "--allow-shared-invalidation",
        action="store_true",
        help="Migration-only: allow invalidation without per-workspace endpoints.",
    )
    parser.add_argument(
        "--lsp-readiness-symbol",
        help="Symbol lookup used to prove a restarted workspace is serving the current revision.",
    )
    parser.add_argument(
        "--lsp-readiness-file",
        help="Project-relative file used for a symbols-overview readiness probe.",
    )
    args = parser.parse_args()

    if not args.allow_non_loopback and args.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        parser.error(
            "--host must be loopback; pass --allow-non-loopback to override"
        )

    projects = args.projects or KUBERNAUT_FAMILY_PROJECTS
    try:
        allowed_roots = _require_allowed_roots(args.allowed_roots)
    except ValueError as exc:
        parser.error(str(exc))

    invalidation_token = None
    if args.invalidation_token_file:
        try:
            invalidation_token = pathlib.Path(
                args.invalidation_token_file
            ).expanduser().read_text().strip()
        except OSError as exc:
            parser.error(f"cannot read --invalidation-token-file: {exc}")
        if not invalidation_token:
            parser.error("--invalidation-token-file is empty")

    invalidation_enabled = bool(invalidation_token or args.invalidation_spool_dir)
    if args.invalidation_spool_dir and not invalidation_token:
        parser.error("--invalidation-spool-dir requires --invalidation-token-file")
    if invalidation_enabled and not (
        args.lsp_readiness_symbol or args.lsp_readiness_file
    ):
        parser.error(
            "invalidation requires --lsp-readiness-symbol or "
            "--lsp-readiness-file"
        )
    require_workspace = args.require_workspace or (
        "enforce" if invalidation_enabled else "off"
    )

    workspace_upstreams: dict[str, str] = {}
    for item in args.workspace_upstreams or []:
        workspace, separator, url = item.partition("=")
        if not separator or not workspace or not url:
            parser.error("--workspace-upstream must be <path>=<url>")
        workspace_upstreams[workspace] = url

    workspace_serena_command = None
    if args.workspace_serena_command:
        import shlex

        workspace_serena_command = shlex.split(args.workspace_serena_command)
        if not any("{port}" in arg for arg in workspace_serena_command):
            parser.error("--workspace-serena-command must contain {port}")

    log.info(
        "starting on %s:%d, upstream=%s, projects=%s, allowed_roots=%s, "
        "invalidation=%s, require_workspace=%s",
        args.host,
        args.port,
        args.upstream_url,
        projects,
        allowed_roots,
        invalidation_enabled,
        require_workspace,
    )
    app = build_app(
        projects,
        args.upstream_url,
        args.upstream_host_header,
        allowed_roots=allowed_roots,
        invalidation_token=invalidation_token,
        invalidation_spool_dir=args.invalidation_spool_dir,
        require_workspace=require_workspace,
        workspace_settle_s=args.workspace_settle_s,
        workspace_upstreams=workspace_upstreams,
        workspace_barrier_dir=args.workspace_barrier_dir,
        workspace_serena_command=workspace_serena_command,
        workspace_serena_idle_ttl_s=args.workspace_serena_idle_ttl_s,
        workspace_serena_port_base=args.workspace_serena_port_base,
        allow_shared_invalidation=args.allow_shared_invalidation,
        lsp_readiness_symbol=args.lsp_readiness_symbol,
        lsp_readiness_file=args.lsp_readiness_file,
    )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
