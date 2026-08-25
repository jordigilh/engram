#!/usr/bin/env python3
"""Aggregates hindsight-docs, hindsight-issues, cocoindex-code, and serena
behind ONE Cursor-facing MCP HTTP mount per repo, instead of the 3-4
separate `.cursor/mcp.json` server entries every onboarded repo had before
this module existed.

Graduated from a single-repo (`praxis-grid`) spike on 2026-08-21 to cover
every onboarded repo across all families (kubernaut, koku, dcm, praxis,
rhdh-plugins, engram itself) after the spike found no fundamental blocker --
see docs/findings/2026-08.md's 2026-08-21 entries for the spike results and
the full-rollout survey/decisions. `build_project_registry()` below is the
single source of truth for what each repo gets.

Background (see the "Engram unified MCP gateway spike" plan and
docs/findings/2026-08.md's many "MCP shows Disabled" entries): most of this
session's recurring MCP-flakiness incidents trace back to either Cursor's
client-side reconnect logic getting stuck on *one* of several live
connections, or a backend daemon restarting mid-session. Both get less
likely to matter the fewer independent connections Cursor has to keep
healthy per repo. `serena_multiplex.py` already proved the core pattern --
one stable Cursor-facing HTTP mount, thin relay to a flakier backend, retry
on backend hiccups -- for a *single* tool family. This module generalizes
that to aggregate **four** families behind one mount.

Two backend transports exist today:
  - hindsight-docs / hindsight-issues are already HTTP (hindsight-api on
    :8888) -- `HttpRelayAdapter` does one-shot POST-only MCP round-trips
    against them, the same "never open a GET/SSE listen stream" approach
    `serena_multiplex.py` uses (see that module's docstring for why: a
    persistent SSE client crashed the shared Serena daemon during the
    2026-08-13 spike -- known, still-open upstream `mcp`/fastmcp bugs).
  - cocoindex-code and serena are currently spawned fresh, per Cursor
    window, as **stdio** subprocesses. `StdioSubprocessAdapter` instead
    spawns each **once** and keeps it alive as this gateway's own
    long-lived child process, talking MCP-over-stdio to it via the `mcp`
    Python SDK's client session -- the same machinery Cursor itself would
    use, just hosted here instead of once per window.

Tool-name collisions: checked the real tool catalogs (see the plan) -- the
*only* collision is hindsight-docs vs hindsight-issues, since they're the
same backend type against different banks and expose identical tool names
(`recall`, `retain`, ...). cocoindex-code's and serena's tool names don't
collide with anything. So only the "docs"/"issues" backend keys get their
tool names prefixed (`docs_recall`, `issues_recall`); "code"/"serena" pass
through unprefixed.

Per-backend degradation is a first-class goal, not an afterthought: a
backend that fails to list its tools (dead subprocess, unreachable HTTP)
is dropped from the merged catalog and logged, but never takes down the
other three families or the gateway process itself. A backend that dies
*after* being listed (subprocess crash between tools/list and tools/call)
surfaces as a clean per-call JSON-RPC error instead of a hang or 500.

Runs as a single supervised launchd service
(`launchd/io.vectorize.engram-gateway.plist`) fronting every registry entry
at once -- see that plist's own header for the KeepAlive/restart rationale
(a bare, unsupervised `engram-gateway` process was found during the spike to
die silently without one, twice, in well under an hour).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Protocol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - engram-gateway - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("engram-gateway")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8896
FORWARD_TIMEOUT_S = 60.0

# Backend keys whose tool names collide across families (both are the same
# hindsight-api backend type, just pointed at a different bank) and
# therefore need disambiguating. Anything not in this set (code, serena)
# passes through unprefixed -- verified empirically that their tool names
# don't collide with each other or with docs/issues (see plan).
PREFIXED_BACKENDS = frozenset({"docs", "issues"})


class BackendAdapter(Protocol):
    """What `aggregate_tools_list`/`handle_tools_call` need from a backend,
    real (HttpRelayAdapter, StdioSubprocessAdapter) or fake (tests)."""

    async def list_tools(self) -> list[dict]: ...

    async def call_tool(self, name: str, arguments: dict) -> dict: ...


def prefixed_tool_name(backend_key: str, raw_name: str) -> str:
    """Aggregated-catalog name for a tool `raw_name` from `backend_key`."""
    if backend_key in PREFIXED_BACKENDS:
        return f"{backend_key}_{raw_name}"
    return raw_name


def build_catalog(per_backend_tools: dict[str, list[dict]]) -> tuple[dict[str, tuple[str, str]], list[dict]]:
    """Merge each backend's raw tool list into one aggregated catalog.

    Returns (catalog, tool_defs):
      - catalog maps the aggregated (possibly-prefixed) tool name to
        (backend_key, raw_tool_name), used by `route_call` to dispatch
        `tools/call`.
      - tool_defs is the list of tool definitions to hand back verbatim in
        a `tools/list` response, with just the `name` field rewritten to
        the aggregated name.

    Fails safe on an unexpected unprefixed-name collision across backends
    (shouldn't happen today -- verified empirically, see module docstring
    -- but a future backend addition could introduce one): the first
    backend processed (dict iteration order) wins, the collision is only
    logged, never raised.
    """
    catalog: dict[str, tuple[str, str]] = {}
    tool_defs: list[dict] = []
    for backend_key, tools in per_backend_tools.items():
        for tool in tools:
            raw_name = tool["name"]
            final_name = prefixed_tool_name(backend_key, raw_name)
            if final_name in catalog:
                log.warning(
                    "tool name collision: %r from backend %r ignored, already owned by backend %r",
                    final_name,
                    backend_key,
                    catalog[final_name][0],
                )
                continue
            catalog[final_name] = (backend_key, raw_name)
            tool_defs.append({**tool, "name": final_name})
    return catalog, tool_defs


def route_call(tool_name: str, catalog: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """Which backend owns `tool_name`, and what its raw (un-prefixed) name
    is there -- or None if unknown (never existed, or its backend was
    dropped from the catalog by `aggregate_tools_list` for being down)."""
    return catalog.get(tool_name)


# Tools rarely exercised by the documented recall/retain/reflect/mental-model
# workflow (hindsight-memory.mdc) or by Serena's symbol/search/edit tools
# (local-mcp-tool-preference.mdc) get dropped from the aggregated catalog
# entirely, not just hidden client-side -- every extra tool definition costs
# context-window budget and counts against Cursor's own active-tool ceiling.
# A single hindsight-api backend's raw catalog is 29 tools (mostly bank/
# document/operation/directive administration never used day-to-day); a repo
# wired to BOTH hindsight-docs and hindsight-issues plus serena's 27 tools
# (kubernaut-family) hit 87 total -- more than double Cursor's ~40-tool
# ceiling, and got stuck permanently `Disabled` no matter how many times it
# was re-enabled (see docs/findings/2026-08.md, 2026-08-22 "MCP shows
# Disabled, root cause: tool-count ceiling" entry). Trimming every hindsight-
# shaped and serena backend to its actually-used subset keeps every onboarded
# repo well under budget without losing any tool actually exercised in
# practice; the dropped administrative tools remain reachable via a direct
# curl against hindsight-api / the serena daemon if ever genuinely needed.
RELEVANT_HINDSIGHT_TOOLS = frozenset(
    {
        "retain",
        "sync_retain",
        "recall",
        "reflect",
        "list_mental_models",
        "get_mental_model",
        "refresh_mental_model",
    }
)

RELEVANT_SERENA_TOOLS = frozenset(
    {
        "replace_content",
        "replace_in_files",
        "replace_symbol_body",
        "insert_after_symbol",
        "insert_before_symbol",
        "search_for_pattern",
        "get_symbols_overview",
        "find_symbol",
        "find_referencing_symbols",
        "find_implementations",
        "find_declaration",
        "get_diagnostics_for_file",
        "rename_symbol",
        "safe_delete_symbol",
        "activate_project",
        "list_queryable_projects",
        "query_project",
    }
)

# Backends with no entry here (code/cocoindex, and any future family) pass
# through unfiltered -- their catalogs are already small (2 tools today).
RELEVANT_TOOLS_BY_BACKEND: dict[str, frozenset[str]] = {
    "docs": RELEVANT_HINDSIGHT_TOOLS,
    "issues": RELEVANT_HINDSIGHT_TOOLS,
    "serena": RELEVANT_SERENA_TOOLS,
}


def filter_relevant_tools(backend_key: str, tools: list[dict]) -> list[dict]:
    """Drop rarely-used administrative tools for backends known to expose an
    oversized catalog. See `RELEVANT_TOOLS_BY_BACKEND`'s module-level comment
    for why this exists."""
    allowed = RELEVANT_TOOLS_BY_BACKEND.get(backend_key)
    if allowed is None:
        return tools
    return [tool for tool in tools if tool["name"] in allowed]


async def _fetch_backend_tools(backend_key: str, adapter: BackendAdapter) -> tuple[str, list[dict] | Exception]:
    try:
        tools = await adapter.list_tools()
        return backend_key, tools
    except Exception as exc:  # noqa: BLE001 - a dead backend must not raise into the caller
        return backend_key, exc


async def aggregate_tools_list(
    backends: dict[str, BackendAdapter],
) -> tuple[list[dict], dict[str, tuple[str, str]], dict[str, str]]:
    """Query every backend's `list_tools()` concurrently, merge the
    survivors into one catalog, and report which backends (if any) failed
    -- without ever raising, so one dead backend can't break `tools/list`
    for the other three."""
    results = await asyncio.gather(*(_fetch_backend_tools(key, adapter) for key, adapter in backends.items()))

    per_backend_tools: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for backend_key, outcome in results:
        if isinstance(outcome, Exception):
            errors[backend_key] = str(outcome)
            log.warning("backend %r failed to list tools: %s", backend_key, outcome)
            continue
        per_backend_tools[backend_key] = filter_relevant_tools(backend_key, outcome)

    catalog, tool_defs = build_catalog(per_backend_tools)
    return tool_defs, catalog, errors


def _jsonrpc_error_result(message_id: Any, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def _jsonrpc_success_result(message_id: Any, backend_result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": backend_result}


async def handle_tools_call(
    message: dict,
    catalog: dict[str, tuple[str, str]],
    backends: dict[str, BackendAdapter],
) -> dict | None:
    """Route a `tools/call` JSON-RPC message to whichever backend owns the
    (aggregated) tool name, returning a well-formed JSON-RPC response.
    Returns None for any other method, so the caller knows to forward it
    generically instead (e.g. `initialize`, `notifications/initialized`).

    Never raises: an unknown tool name and a backend that throws mid-call
    both come back as a normal (isError=True) tool result instead of an
    exception, so a single dead/unknown-tool call can't take down the
    connection the way an unhandled exception in the ASGI endpoint would.
    """
    if message.get("method") != "tools/call":
        return None

    message_id = message.get("id")
    params = message.get("params") or {}
    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}

    routed = route_call(tool_name, catalog)
    if routed is None:
        return _jsonrpc_error_result(
            message_id,
            f"Unknown tool: {tool_name!r} (never existed, or its backend is currently down)",
        )

    backend_key, raw_name = routed
    adapter = backends.get(backend_key)
    if adapter is None:
        return _jsonrpc_error_result(message_id, f"Backend {backend_key!r} for tool {tool_name!r} is not wired up")

    try:
        result = await adapter.call_tool(raw_name, arguments)
    except Exception as exc:  # noqa: BLE001 - degrade to a clean tool error, don't crash the gateway
        log.warning("backend %r failed on tools/call(%r): %s", backend_key, raw_name, exc)
        return _jsonrpc_error_result(message_id, f"backend {backend_key!r} failed: {exc}")

    return _jsonrpc_success_result(message_id, result)


# ---------------------------------------------------------------------------
# Real backend adapters (not exercised by the unit tests above -- those use
# FakeAdapter; these are validated live during the plan's gateway-verify /
# gateway-degradation-test steps).
# ---------------------------------------------------------------------------


def _parse_sse_json(body: bytes) -> dict:
    """Same SSE-or-plain-JSON parsing as serena_multiplex.py -- duplicated
    rather than imported so this spike module has no dependency on that
    one; worth de-duplicating into a shared helper if this graduates past
    spike status."""
    text = body.decode("utf-8", errors="replace").strip()
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    return json.loads(text)


class HttpRelayAdapter:
    """Backend adapter for an already-HTTP MCP server (hindsight-docs,
    hindsight-issues). Each call is a fresh one-shot MCP session
    (initialize -> notifications/initialized -> the real call -> delete),
    matching serena_multiplex.py's "POST-only, never GET/SSE" design to
    avoid the upstream mcp/fastmcp SSE-reconnect bug documented there."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def _roundtrip(self, method: str, params: dict | None = None) -> dict:
        import httpx

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT_S) as client:
            init_resp = await client.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "engram-gateway", "version": "0.0.1"},
                    },
                },
                headers=headers,
            )
            init_resp.raise_for_status()
            session_id = init_resp.headers.get("mcp-session-id")
            session_headers = {**headers, "mcp-session-id": session_id} if session_id else headers

            await client.post(
                self.url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=session_headers,
            )

            call_resp = await client.post(
                self.url,
                json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}},
                headers=session_headers,
            )
            call_resp.raise_for_status()
            result = _parse_sse_json(call_resp.content)

            if session_id:
                await client.delete(self.url, headers=session_headers)

        if "error" in result:
            raise RuntimeError(f"{method} failed: {result['error']}")
        return result.get("result", {})

    async def list_tools(self) -> list[dict]:
        result = await self._roundtrip("tools/list")
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._roundtrip("tools/call", {"name": name, "arguments": arguments})


class StdioSubprocessAdapter:
    """Backend adapter that spawns a stdio MCP server (cocoindex-code,
    serena) ONCE and keeps it alive as this gateway's own long-lived child
    process, instead of Cursor spawning a fresh one per window. Calls are
    serialized behind one lock per adapter (same single-writer precedent as
    serena_multiplex.py's ActiveProjectTracker) since a stdio MCP session is
    not safe for concurrent use."""

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None) -> None:
        self.command = command
        self.args = args
        self.env = env
        self._lock = asyncio.Lock()
        self._session = None
        self._exit_stack = None

    async def _ensure_started(self) -> None:
        if self._session is not None:
            return

        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._exit_stack = AsyncExitStack()
        params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session

    async def _restart(self) -> None:
        """Drop the dead session/exit-stack and spawn a fresh one.

        Deliberately does NOT call `self._exit_stack.aclose()` here: each
        Starlette/uvicorn request is its own asyncio task, so the exit stack
        (built by whichever earlier request's task first called
        `_ensure_started`) has anyio cancel scopes bound to *that* task --
        closing it from a *different* task (this one, reached because the
        subprocess died and a later request's call_tool is retrying) raises
        "Attempted to exit cancel scope in a different task than it was
        entered in" (verified live during the spike's degradation test).
        Since we only ever reach this path because the underlying process is
        already dead/broken, there is nothing left to cleanly close anyway
        -- just abandon the old stack and let a fresh `_ensure_started()`
        build a new one from *this* task, which is then self-consistent for
        whichever task closes it next."""
        self._session = None
        self._exit_stack = None
        await self._ensure_started()

    async def list_tools(self) -> list[dict]:
        async with self._lock:
            await self._ensure_started()
            result = await self._session.list_tools()
            return [
                {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
                for t in result.tools
            ]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        async with self._lock:
            await self._ensure_started()
            try:
                result = await self._session.call_tool(name, arguments)
            except Exception:
                # The subprocess may have died between calls -- restart once
                # and retry, same one-retry-only philosophy as
                # serena_multiplex._forward_scoped.
                await self._restart()
                result = await self._session.call_tool(name, arguments)
            return {
                # exclude_none=True matters: mcp SDK content models (TextContent
                # etc.) have optional fields like `annotations`/`meta` that
                # default to None, and a bare model_dump() serializes those as
                # explicit `null` rather than omitting the key. Several MCP
                # clients' schemas require `annotations` to be either a real
                # object or absent -- `null` fails validation and the client
                # discards the whole result before the caller ever sees the
                # data, surfacing as a generic "backend is currently down"
                # (see docs/findings/2026-08.md, 2026-08-25 entry).
                "content": [c.model_dump(exclude_none=True) if hasattr(c, "model_dump") else c for c in result.content],
                "isError": result.isError,
            }


_prewarm_tasks: set[asyncio.Task] = set()


async def _prewarm_stdio_backends(projects: dict[str, dict[str, BackendAdapter]]) -> None:
    """Fire off a background `list_tools()` for every stdio backend (Serena,
    cocoindex-code) right at gateway startup, without blocking the gateway
    from accepting other traffic while these are still warming up.

    Why this exists: most MCP clients (Cursor included) call `tools/list`
    exactly once per session, right after `initialize`, and cache the
    result for the life of that connection -- there's no server-initiated
    `notifications/tools/list_changed` push from this gateway to make a
    client re-fetch later. So if that one `tools/list` call happens to race
    a cold LSP/language-server startup (which can take several seconds) and
    gets cancelled or returns a reduced tool set, the client is stuck with
    that degraded snapshot until its *next* fresh reconnect -- no amount of
    "disable/enable" or "restart Cursor" helps if the reconnect's own
    `tools/list` call lands in the same race window again. Pre-warming here
    means that by the time any real client connects (almost always more
    than a few seconds after this process starts), the subprocess is
    already up and `list_tools()` returns immediately. See
    docs/findings/2026-08.md, 2026-08-25 entry (`praxis-grid` stuck on a
    16-tool snapshot from exactly this race, right after a gateway restart)."""

    async def _warm_one(label: str, adapter: BackendAdapter) -> None:
        try:
            await adapter.list_tools()
            log.info("pre-warmed backend %r", label)
        except Exception:
            log.warning(
                "pre-warm failed for backend %r -- will retry lazily on first real request", label, exc_info=True
            )

    for project, backends in projects.items():
        for key, adapter in backends.items():
            if isinstance(adapter, StdioSubprocessAdapter):
                task = asyncio.create_task(_warm_one(f"{project}/{key}", adapter))
                _prewarm_tasks.add(task)
                task.add_done_callback(_prewarm_tasks.discard)


def build_app(projects: dict[str, dict[str, BackendAdapter]]):
    """Build the Starlette app: one route per project, each aggregating
    that project's own set of backend adapters. `projects` maps project
    name -> {backend_key: BackendAdapter}."""
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    # Cache of (catalog, backends) built lazily per project on first
    # tools/list, refreshed whenever a new tools/list comes in -- backends
    # that flap back up are picked up on the next list without a restart.
    state: dict[str, dict] = {name: {"catalog": {}, "backends": backends} for name, backends in projects.items()}

    @asynccontextmanager
    async def _lifespan(_app):
        # Scheduled, not awaited-to-completion: returns almost immediately
        # so the gateway can start accepting requests for other projects
        # while these stdio backends warm up in the background.
        asyncio.create_task(_prewarm_stdio_backends(projects))
        yield

    def _make_endpoint(project: str):
        async def endpoint(request: Request) -> Response:
            if request.method == "GET":
                return Response(status_code=405)

            body = await request.body()
            try:
                message = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return Response(status_code=400)

            backends = state[project]["backends"]
            method = message.get("method")

            if method == "tools/list":
                tool_defs, catalog, errors = await aggregate_tools_list(backends)
                state[project]["catalog"] = catalog
                if errors:
                    log.warning("project %r: backends unavailable this round: %s", project, errors)
                result = {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": tool_defs}}
                return Response(
                    content=f"event: message\ndata: {json.dumps(result)}\n\n",
                    media_type="text/event-stream",
                )

            if method == "tools/call":
                response = await handle_tools_call(message, state[project]["catalog"], backends)
                return Response(
                    content=f"event: message\ndata: {json.dumps(response)}\n\n",
                    media_type="text/event-stream",
                )

            if message.get("id") is not None:
                # initialize and any other request we don't special-case:
                # acknowledge minimally rather than hang the caller.
                result = {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "engram-gateway", "version": "0.0.1"},
                    },
                }
                return Response(
                    content=f"event: message\ndata: {json.dumps(result)}\n\n",
                    media_type="text/event-stream",
                )

            # A notification (no id, e.g. notifications/initialized) -- MCP
            # requires no response body.
            return Response(status_code=202)

        return endpoint

    routes = [Route(f"/mcp/{project}", _make_endpoint(project), methods=["GET", "POST", "DELETE"]) for project in projects]
    return Starlette(routes=routes, lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Full rollout registry (2026-08-21): every onboarded repo, config only (no
# I/O, no adapter instantiation -- see `build_backend_adapters` for that).
# Deliberately preserves each repo's *existing* backend set exactly rather
# than normalizing towards a uniform 4-backend shape: several repos are
# missing one or more backends today (kubernaut-console has no serena,
# kubernaut-docs has no cocoindex-code, engram itself has neither issues nor
# serena) and this registry must keep reflecting that, not silently add
# capabilities a repo never had. See docs/findings/2026-08.md's 2026-08-21
# rollout entry for the full survey this was built from.
#
# Explicitly excluded (see that entry for why): kubernaut-demo-scenarios (no
# .cursor/mcp.json today -- nothing to consolidate) and the two
# kubernaut-fix-1995-* scratch worktrees (serena still points directly at
# the raw upstream daemon on :8892 rather than through the :8893 multiplex,
# a stale/pre-multiplex config on what look like abandoned one-off
# branch-fix clones -- not guessed at here).
# ---------------------------------------------------------------------------

_HINDSIGHT_BASE = "http://localhost:8888"
_PG_URL = "postgresql://hindsight:hindsight@localhost:5432/hindsight"

DCM_REPOS = [
    "udlm",
    "k8s-container-service-provider",
    "three-tier-app-demo-service-provider",
    "quadlet-deploy",
    "acm-cluster-service-provider",
    "shared-workflows",
    "kubevirt-service-provider",
    "cli",
    "utilities",
    "osac-service-provider",
    "enhancements",
    "dcm-project.github.io",
]
PRAXIS_REPOS_WITH_SERENA = [
    "praxis",
    "praxis-ai",
    "praxis-demos",
    "praxis-forge",
    "praxis-grid",
    "praxis-operator",
    "praxis-policy",
]
PRAXIS_REPOS_WITHOUT_SERENA = ["praxis-conventions", "praxis-experiments", "praxis-proxy.github.io"]


def _hindsight(bank: str) -> dict:
    return {"kind": "http", "url": f"{_HINDSIGHT_BASE}/mcp/{bank}/"}


def _http(url: str) -> dict:
    return {"kind": "http", "url": url}


def _stdio(command: str, args: list[str] | None = None, env: dict | None = None, shared_key: str | None = None) -> dict:
    spec = {"kind": "stdio", "command": command, "args": args or [], "env": env}
    if shared_key is not None:
        spec["shared_key"] = shared_key
    return spec


def _serena_stdio(home: str, workspace: str) -> dict:
    return _stdio(
        f"{home}/.local/bin/uvx",
        [
            "--from",
            "git+https://github.com/oraios/serena",
            "serena",
            "start-mcp-server",
            "--project",
            workspace,
            "--context",
            "ide",
            "--add-mode",
            "no-memories",
            "--open-web-dashboard",
            "false",
        ],
    )


def build_project_registry(home: str) -> dict[str, dict[str, dict]]:
    """project_name -> {backend_key: backend_spec}, pure config derived from
    the 2026-08-21 survey of every onboarded repo's actual .cursor/mcp.json.
    `home` is injected (rather than read from `os.path.expanduser` here) so
    this stays a pure, easily-testable function."""
    venv_bin = f"{home}/.hindsight/venv/bin"
    registry: dict[str, dict[str, dict]] = {}

    kubernaut_http_code = _http("http://127.0.0.1:8891/mcp")

    def kubernaut_serena(project: str) -> dict:
        return _http(f"http://127.0.0.1:8893/mcp/{project}")

    for name in ("kubernaut", "kubernaut-operator", "kubernaut-v1.5", "kubernaut-v1.6"):
        registry[name] = {
            "docs": _hindsight("kubernaut-docs"),
            "issues": _hindsight("kubernaut-issues"),
            "code": kubernaut_http_code,
            "serena": kubernaut_serena(name),
        }
    registry["kubernaut-console"] = {
        "docs": _hindsight("kubernaut-docs"),
        "issues": _hindsight("kubernaut-issues"),
        "code": _stdio(f"{venv_bin}/python3", [f"{home}/.hindsight/cocoindex-search.py"], {"COCOINDEX_PG_URL": _PG_URL}),
    }
    registry["kubernaut-docs"] = {
        "docs": _hindsight("kubernaut-docs"),
        "issues": _hindsight("kubernaut-issues"),
        "serena": kubernaut_serena("kubernaut-docs"),
    }

    koku_code_stdio = _stdio(f"{venv_bin}/engram-search-koku", env={"COCOINDEX_PG_URL": _PG_URL}, shared_key="koku-code")
    for name in ("koku", "koku-service-operator"):
        registry[name] = {
            "docs": _hindsight("koku-docs"),
            "issues": _hindsight("koku-issues"),
            "code": koku_code_stdio,
            "serena": _http(f"http://127.0.0.1:8895/mcp/{name}"),
        }
    registry["koku-insights-onprem"] = {
        "docs": _hindsight("koku-docs"),
        "issues": _hindsight("koku-issues"),
        "code": koku_code_stdio,
        "serena": _serena_stdio(home, f"{home}/go/src/github.com/insights-onprem/koku"),
    }

    dcm_code_stdio = _stdio(
        f"{venv_bin}/engram-search-dcm", env={"COCOINDEX_PG_URL": _PG_URL, "HF_HUB_OFFLINE": "1"}, shared_key="dcm-code"
    )
    for repo in DCM_REPOS:
        registry[f"dcm-{repo}"] = {
            "docs": _hindsight("dcm-docs"),
            "issues": _hindsight("dcm-issues"),
            "code": dcm_code_stdio,
            "serena": _serena_stdio(home, f"{home}/go/src/github.com/dcm-project/{repo}"),
        }

    praxis_code_stdio = _stdio(f"{venv_bin}/engram-search-praxis", env={"COCOINDEX_PG_URL": _PG_URL}, shared_key="praxis-code")
    for repo in PRAXIS_REPOS_WITH_SERENA:
        registry[repo] = {
            "docs": _hindsight("praxis-docs"),
            "issues": _hindsight("praxis-issues"),
            "code": praxis_code_stdio,
            "serena": _serena_stdio(home, f"{home}/go/src/github.com/praxis-proxy/{repo}"),
        }
    for repo in PRAXIS_REPOS_WITHOUT_SERENA:
        registry[repo.replace(".", "-")] = {
            "docs": _hindsight("praxis-docs"),
            "issues": _hindsight("praxis-issues"),
            "code": praxis_code_stdio,
        }

    registry["rhdh-plugins"] = {
        "docs": _hindsight("rhdh-plugins-docs"),
        "issues": _hindsight("rhdh-plugins-issues"),
        "code": _stdio(f"{venv_bin}/engram-search-rhdh-plugins", env={"COCOINDEX_PG_URL": _PG_URL}),
        "serena": _serena_stdio(home, f"{home}/go/src/github.com/redhat-developer/rhdh-plugins"),
    }

    registry["engram"] = {
        "docs": _hindsight("engram-docs"),
        "code": _stdio(f"{venv_bin}/engram-search-engram", env={"COCOINDEX_PG_URL": _PG_URL}),
    }

    return registry


def build_backend_adapters(registry: dict[str, dict[str, dict]]) -> dict[str, dict[str, BackendAdapter]]:
    """Instantiate real adapters from registry specs (still no I/O -- both
    adapter types connect lazily on first use). Stdio specs carrying the
    same `shared_key` (the family-wide cocoindex-code search binaries,
    which are stateless w.r.t. which repo mount is asking) collapse to one
    shared `StdioSubprocessAdapter` instance instead of one per project --
    unlike serena, which is bound to one repo's filesystem via `--project`
    and can never be shared."""
    shared_stdio_cache: dict[str, StdioSubprocessAdapter] = {}
    adapters: dict[str, dict[str, BackendAdapter]] = {}

    for project, specs in registry.items():
        adapters[project] = {}
        for backend_key, spec in specs.items():
            if spec["kind"] == "http":
                adapters[project][backend_key] = HttpRelayAdapter(spec["url"])
                continue

            shared_key = spec.get("shared_key")
            if shared_key is not None:
                if shared_key not in shared_stdio_cache:
                    shared_stdio_cache[shared_key] = StdioSubprocessAdapter(spec["command"], spec["args"], spec["env"])
                adapters[project][backend_key] = shared_stdio_cache[shared_key]
            else:
                adapters[project][backend_key] = StdioSubprocessAdapter(spec["command"], spec["args"], spec["env"])

    return adapters


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    import os

    home = os.path.expanduser("~")
    registry = build_project_registry(home)
    projects = build_backend_adapters(registry)
    log.info("starting on %s:%d, %d projects: %s", args.host, args.port, len(projects), sorted(projects))
    app = build_app(projects)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
