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
lifecycle -- see PrefectHQ/fastmcp#532, #3025, #671). Plain one-shot
POST-only HTTP calls (proven safe in this same spike via curl, which never
opens a GET at all) don't hit this bug. This module therefore does NOT use
fastmcp's Client/create_proxy for the upstream connection -- it hand-rolls a
minimal one-shot httpx POST relay instead, and returns 405 for any GET
against its own downstream-facing endpoint (spec-compliant per the MCP
Streamable HTTP transport spec: a server that never pushes unsolicited
messages may decline the SSE listen stream with 405 rather than opening one).

Not tied to "kubernaut-family" specifically -- `projects` and `upstream_url`
are both parameters, so any other Serena daemon shared across a repo family
could reuse this same module.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Awaitable, Callable

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
    """Classify a tools/call target: "intercept" (activate_project itself --
    redundant/dangerous to forward, this mount's project is already fixed),
    "agnostic" (safe to forward untouched, no activation needed), or
    "scoped" (default -- must pin this mount's project before forwarding)."""
    if tool_name == "activate_project":
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


def _intercept_response(message: dict, project: str) -> dict:
    """Synthesize a well-formed JSON-RPC tool result for an agent's own
    (redundant) activate_project call, without contacting upstream at all."""
    return {
        "jsonrpc": "2.0",
        "id": message.get("id"),
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"This mount is permanently pinned to project "
                        f"'{project}' -- activate_project is managed "
                        f"automatically and does not need to be called."
                    ),
                }
            ],
            "isError": False,
        },
    }


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


def _make_activate(upstream_url: str) -> Callable[[str], Awaitable[None]]:
    """Build the `activate` callable for ActiveProjectTracker.pinned(): a
    short-lived, one-shot (POST-only, never GET) control session against the
    upstream daemon -- see module docstring for why GET/persistent-SSE
    clients are avoided here."""

    async def activate(project: str) -> None:
        import httpx

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
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
                        "clientInfo": {"name": "serena-multiplex-activate", "version": "0.0.1"},
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
                    "params": {"name": "activate_project", "arguments": {"project": project}},
                },
                headers=session_headers,
            )
            call_resp.raise_for_status()
            result = _parse_sse_json(call_resp.content)
            if result.get("result", {}).get("isError"):
                raise RuntimeError(f"activate_project({project!r}) failed: {result}")

            if session_id:
                await client.delete(upstream_url, headers=session_headers)

    return activate


def build_app(projects: list[str], upstream_url: str):
    """Build the Starlette app: one route per project, each forwarding to
    the shared upstream via one-shot POST relays with activate_project
    injected ahead of project-scoped tool calls (see module docstring)."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    tracker = ActiveProjectTracker()
    activate = _make_activate(upstream_url)

    # Headers that are connection/host-specific and must not be blindly
    # relayed between the two hops of this proxy.
    _DROP_REQUEST_HEADERS = {"host", "content-length", "connection"}
    _DROP_RESPONSE_HEADERS = {"content-length", "connection", "transfer-encoding"}

    def _make_endpoint(project: str):
        async def endpoint(request: Request) -> Response:
            if request.method == "GET":
                # Spec-compliant decline of the SSE listen stream -- see
                # module docstring for why this proxy never opens one.
                return Response(status_code=405)

            fwd_headers = {
                k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS
            }

            async def forward_raw() -> Response:
                import httpx

                async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT_S) as client:
                    upstream_resp = await client.request(
                        request.method,
                        upstream_url,
                        content=await request.body(),
                        headers=fwd_headers,
                    )
                resp_headers = {
                    k: v
                    for k, v in upstream_resp.headers.items()
                    if k.lower() not in _DROP_RESPONSE_HEADERS
                }
                return Response(
                    content=upstream_resp.content,
                    status_code=upstream_resp.status_code,
                    headers=resp_headers,
                )

            if request.method != "POST":
                return await forward_raw()

            body = await request.body()
            try:
                message = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return await forward_raw()

            if message.get("method") != "tools/call":
                return await forward_raw()

            tool_name = (message.get("params") or {}).get("name", "")
            route = route_tool_call(tool_name)

            if route == "intercept":
                result = _intercept_response(message, project)
                return Response(
                    content=f"event: message\ndata: {json.dumps(result)}\n\n",
                    media_type="text/event-stream",
                )

            if route == "agnostic":
                return await forward_raw()

            try:
                return await _forward_scoped(project, tracker, activate, forward_raw)
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

    routes = [Route(f"/mcp/{project}", _make_endpoint(project), methods=["GET", "POST", "DELETE"]) for project in projects]
    return Starlette(routes=routes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--upstream-url", default=DEFAULT_UPSTREAM_URL)
    parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        help="Repeatable. Defaults to the kubernaut-family project list.",
    )
    args = parser.parse_args()

    projects = args.projects or KUBERNAUT_FAMILY_PROJECTS
    log.info(
        "starting on %s:%d, upstream=%s, projects=%s",
        args.host,
        args.port,
        args.upstream_url,
        projects,
    )
    app = build_app(projects, args.upstream_url)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
