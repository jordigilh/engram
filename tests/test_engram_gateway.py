"""Tests for engram_gateway.py -- the spike aggregating hindsight-docs,
hindsight-issues, cocoindex-code, and serena behind one Cursor-facing MCP
HTTP mount per repo (see the "Engram unified MCP gateway spike" plan).

Business problem under test: today each onboarded repo's `.cursor/mcp.json`
lists 3-4 *separate* MCP server entries, each its own live connection Cursor
has to keep healthy -- the more independent connections, the more surface
area for the recurring "MCP shows Disabled" failure class this session spent
a lot of time on (client-side stale reconnect state, backend daemon
restarts, etc.). This module collapses those into one HTTP mount that
aggregates all four tool families' catalogs and routes `tools/call` to
whichever backend owns the (possibly renamed) tool.

Two real backend transports exist today: hindsight-docs/issues are already
HTTP (hindsight-api), while cocoindex-code and serena are stdio subprocesses
spawned fresh per Cursor window. Only the *pure* aggregation/routing/
degradation logic is unit-tested here, with fake backend adapters standing
in for real HTTP/subprocess I/O -- exactly the same "no real daemon, no real
network" testing approach `test_serena_multiplex.py` uses for its own
routing/retry logic. Live verification against the real backends is a
separate, manual step (see the plan's "gateway-verify" to-do), not part of
this automated suite.
"""
from __future__ import annotations

import asyncio


class FakeAdapter:
    """Stands in for a real BackendAdapter (HttpRelayAdapter /
    StdioSubprocessAdapter) in tests: returns canned tool lists / call
    results, or raises to simulate a dead/unreachable backend."""

    def __init__(self, tools=None, list_error=None, call_results=None, call_error=None):
        self.tools = tools or []
        self.list_error = list_error
        self.call_results = call_results or {}
        self.call_error = call_error
        self.list_calls = 0
        self.call_log: list[tuple[str, dict]] = []

    async def list_tools(self):
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return self.tools

    async def call_tool(self, name, arguments):
        self.call_log.append((name, arguments))
        if self.call_error is not None:
            raise self.call_error
        return self.call_results.get(name, {"content": [{"type": "text", "text": f"ok:{name}"}], "isError": False})


def _tool(name: str, description: str = "") -> dict:
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": {}}}


class TestPrefixedToolName:
    def test_docs_and_issues_tools_get_prefixed(self, engram_gateway):
        assert engram_gateway.prefixed_tool_name("docs", "recall") == "docs_recall"
        assert engram_gateway.prefixed_tool_name("issues", "recall") == "issues_recall"

    def test_code_and_serena_tools_pass_through_unprefixed(self, engram_gateway):
        assert engram_gateway.prefixed_tool_name("code", "praxis_code_search") == "praxis_code_search"
        assert engram_gateway.prefixed_tool_name("serena", "find_symbol") == "find_symbol"


class TestBuildCatalog:
    def test_unprefixed_backends_use_raw_name(self, engram_gateway):
        catalog, _tool_defs = engram_gateway.build_catalog(
            {"code": [_tool("praxis_code_search")], "serena": [_tool("find_symbol")]}
        )

        assert catalog == {
            "praxis_code_search": ("code", "praxis_code_search"),
            "find_symbol": ("serena", "find_symbol"),
        }

    def test_docs_and_issues_backends_get_prefixed_names(self, engram_gateway):
        catalog, _tool_defs = engram_gateway.build_catalog(
            {"docs": [_tool("recall")], "issues": [_tool("recall")]}
        )

        assert catalog == {
            "docs_recall": ("docs", "recall"),
            "issues_recall": ("issues", "recall"),
        }

    def test_tool_defs_carry_the_aggregated_name_not_the_raw_one(self, engram_gateway):
        _catalog, tool_defs = engram_gateway.build_catalog({"docs": [_tool("recall", "Store stuff")]})

        assert tool_defs == [{"name": "docs_recall", "description": "Store stuff", "inputSchema": {"type": "object", "properties": {}}}]

    def test_empty_backends_produce_empty_catalog(self, engram_gateway):
        catalog, tool_defs = engram_gateway.build_catalog({})

        assert catalog == {}
        assert tool_defs == []

    def test_duplicate_unprefixed_name_across_backends_keeps_first_and_does_not_raise(self, engram_gateway):
        """code and serena are both unprefixed -- if they ever define the
        same tool name (shouldn't happen today, verified empirically in the
        plan, but must fail safe rather than silently overwrite/crash), the
        first backend processed wins and the collision is only logged."""
        catalog, tool_defs = engram_gateway.build_catalog(
            {"code": [_tool("shared_name")], "serena": [_tool("shared_name")]}
        )

        assert catalog == {"shared_name": ("code", "shared_name")}
        assert len(tool_defs) == 1


class TestRouteCall:
    def test_known_prefixed_tool_routes_and_strips_prefix(self, engram_gateway):
        catalog = {"docs_recall": ("docs", "recall")}

        assert engram_gateway.route_call("docs_recall", catalog) == ("docs", "recall")

    def test_known_unprefixed_tool_routes_with_unchanged_name(self, engram_gateway):
        catalog = {"find_symbol": ("serena", "find_symbol")}

        assert engram_gateway.route_call("find_symbol", catalog) == ("serena", "find_symbol")

    def test_unknown_tool_returns_none(self, engram_gateway):
        assert engram_gateway.route_call("nonexistent_tool", {}) is None


class TestAggregateToolsList:
    def test_merges_catalogs_from_all_healthy_backends(self, engram_gateway):
        backends = {
            "docs": FakeAdapter(tools=[_tool("recall")]),
            "issues": FakeAdapter(tools=[_tool("recall")]),
            "code": FakeAdapter(tools=[_tool("praxis_code_search")]),
            "serena": FakeAdapter(tools=[_tool("find_symbol")]),
        }

        tool_defs, catalog, errors = asyncio.run(engram_gateway.aggregate_tools_list(backends))

        names = {t["name"] for t in tool_defs}
        assert names == {"docs_recall", "issues_recall", "praxis_code_search", "find_symbol"}
        assert catalog["docs_recall"] == ("docs", "recall")
        assert errors == {}

    def test_backend_failure_is_excluded_but_others_still_returned(self, engram_gateway):
        backends = {
            "docs": FakeAdapter(tools=[_tool("recall")]),
            "code": FakeAdapter(list_error=RuntimeError("engram-search-praxis crashed")),
        }

        tool_defs, catalog, errors = asyncio.run(engram_gateway.aggregate_tools_list(backends))

        names = {t["name"] for t in tool_defs}
        assert names == {"docs_recall"}
        assert all(backend != "code" for backend, _ in catalog.values())
        assert "code" in errors
        assert "crashed" in errors["code"]

    def test_all_backends_failing_returns_empty_catalog_not_raise(self, engram_gateway):
        backends = {
            "docs": FakeAdapter(list_error=RuntimeError("down")),
            "serena": FakeAdapter(list_error=RuntimeError("down too")),
        }

        tool_defs, catalog, errors = asyncio.run(engram_gateway.aggregate_tools_list(backends))

        assert tool_defs == []
        assert catalog == {}
        assert set(errors) == {"docs", "serena"}


class TestHandleToolsCall:
    def test_routes_prefixed_call_and_strips_prefix_before_calling_backend(self, engram_gateway):
        docs = FakeAdapter(call_results={"recall": {"content": [{"type": "text", "text": "found it"}], "isError": False}})
        catalog = {"docs_recall": ("docs", "recall")}
        message = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "docs_recall", "arguments": {"query": "x"}}}

        result = asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"docs": docs}))

        assert docs.call_log == [("recall", {"query": "x"})]
        assert result["id"] == 5
        assert result["result"]["content"][0]["text"] == "found it"

    def test_routes_unprefixed_call_with_unchanged_name(self, engram_gateway):
        serena = FakeAdapter(call_results={"find_symbol": {"content": [], "isError": False}})
        catalog = {"find_symbol": ("serena", "find_symbol")}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "find_symbol", "arguments": {}}}

        asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"serena": serena}))

        assert serena.call_log == [("find_symbol", {})]

    def test_unknown_tool_returns_jsonrpc_error_without_raising(self, engram_gateway):
        message = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "ghost_tool", "arguments": {}}}

        result = asyncio.run(engram_gateway.handle_tools_call(message, {}, {}))

        assert result["id"] == 9
        assert result["result"]["isError"] is True
        assert "ghost_tool" in result["result"]["content"][0]["text"]

    def test_backend_call_exception_returns_jsonrpc_error_without_raising(self, engram_gateway):
        """Degradation at call time: a backend that died *after* it was
        listed (e.g. subprocess crashed between tools/list and tools/call)
        must surface as a clean per-call error, not a 500 / hung connection
        that could be mistaken for the whole gateway being down."""
        dead = FakeAdapter(call_error=RuntimeError("subprocess exited"))
        catalog = {"praxis_code_search": ("code", "praxis_code_search")}
        message = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "praxis_code_search", "arguments": {"query": "foo"}},
        }

        result = asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"code": dead}))

        assert result["id"] == 2
        assert result["result"]["isError"] is True
        assert "subprocess exited" in result["result"]["content"][0]["text"]

    def test_non_tools_call_method_returns_none_so_caller_can_forward_generically(self, engram_gateway):
        message = {"jsonrpc": "2.0", "method": "notifications/initialized"}

        result = asyncio.run(engram_gateway.handle_tools_call(message, {}, {}))

        assert result is None


class TestBuildProjectRegistry:
    """Pure config -- no I/O, no adapter instantiation -- covering the full
    rollout across every onboarded repo (see docs/findings/2026-08.md,
    2026-08-21 rollout entry). Backend heterogeneity is real and must be
    preserved exactly, not normalized away: kubernaut-family already has
    everything on shared HTTP daemons; koku-family's cocoindex-code is
    still stdio while its docs/issues/serena are HTTP; dcm/praxis/
    rhdh-plugins/koku-insights-onprem run cocoindex-code and serena as
    per-family-shared / per-repo stdio respectively; a few repos are
    missing one or more backends entirely (kubernaut-console has no
    serena, kubernaut-docs has no cocoindex-code, engram itself has
    neither issues nor serena) -- the registry must reflect exactly what
    each repo already has today, never add a backend that isn't already
    configured for it."""

    def test_covers_every_onboarded_project(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        assert len(registry) == 33

    def test_kubernaut_family_is_fully_http_already(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        spec = registry["kubernaut-operator"]
        assert spec["docs"] == {"kind": "http", "url": "http://localhost:8888/mcp/kubernaut-docs/"}
        assert spec["issues"] == {"kind": "http", "url": "http://localhost:8888/mcp/kubernaut-issues/"}
        assert spec["code"] == {"kind": "http", "url": "http://127.0.0.1:8891/mcp"}
        assert spec["serena"] == {"kind": "http", "url": "http://127.0.0.1:8893/mcp/kubernaut-operator"}

    def test_kubernaut_console_has_no_serena(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        assert "serena" not in registry["kubernaut-console"]
        assert set(registry["kubernaut-console"]) == {"docs", "issues", "code"}

    def test_kubernaut_docs_has_no_cocoindex_code(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        assert set(registry["kubernaut-docs"]) == {"docs", "issues", "serena"}

    def test_engram_itself_has_only_docs_and_code(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        assert set(registry["engram"]) == {"docs", "code"}
        assert registry["engram"]["docs"]["url"] == "http://localhost:8888/mcp/engram-docs/"

    def test_koku_family_code_is_stdio_but_serena_is_http(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        spec = registry["koku-service-operator"]
        assert spec["code"]["kind"] == "stdio"
        assert spec["code"]["shared_key"] == "koku-code"
        assert spec["serena"] == {"kind": "http", "url": "http://127.0.0.1:8895/mcp/koku-service-operator"}

    def test_koku_insights_onprem_serena_is_still_standalone_stdio(self, engram_gateway):
        """Confirmed unmigrated (see docs/findings/2026-08.md) -- must not
        be silently upgraded to an HTTP multiplex URL it doesn't have."""
        registry = engram_gateway.build_project_registry("/home/u")

        spec = registry["koku-insights-onprem"]
        assert spec["serena"]["kind"] == "stdio"
        assert "shared_key" not in spec["serena"]

    def test_dcm_repos_share_one_cocoindex_backend_but_each_has_its_own_serena(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        cli_spec = registry["dcm-cli"]
        utilities_spec = registry["dcm-utilities"]
        assert cli_spec["code"]["shared_key"] == "dcm-code"
        assert utilities_spec["code"]["shared_key"] == "dcm-code"
        assert cli_spec["code"] == utilities_spec["code"]
        assert cli_spec["serena"]["args"][cli_spec["serena"]["args"].index("--project") + 1] == "/home/u/go/src/github.com/dcm-project/cli"
        assert utilities_spec["serena"]["args"][utilities_spec["serena"]["args"].index("--project") + 1] == "/home/u/go/src/github.com/dcm-project/utilities"

    def test_praxis_repos_share_one_cocoindex_backend(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        assert registry["praxis-grid"]["code"]["shared_key"] == "praxis-code"
        assert registry["praxis-ai"]["code"]["shared_key"] == "praxis-code"

    def test_praxis_repos_without_serena_omit_it(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        assert "serena" not in registry["praxis-conventions"]
        assert "serena" not in registry["praxis-proxy-github-io"]
        assert "serena" in registry["praxis-grid"]

    def test_rhdh_plugins_registry_only_covers_the_four_engram_backends(self, engram_gateway):
        """rhdh-plugins' real .cursor/mcp.json has other, unrelated MCP
        servers (jira/argocd/gitea/kubernetes/orchestrator, several with
        live credentials) alongside the four engram-owned ones -- the
        registry must describe only what this gateway itself is
        responsible for aggregating, never those unrelated entries."""
        registry = engram_gateway.build_project_registry("/home/u")

        assert set(registry["rhdh-plugins"]) == {"docs", "issues", "code", "serena"}

    def test_dcm_code_backend_sets_hf_hub_offline(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        assert registry["dcm-cli"]["code"]["env"]["HF_HUB_OFFLINE"] == "1"


class TestBuildBackendAdapters:
    """Real adapter instantiation from registry specs -- still no I/O
    (adapters connect lazily), but this is where shared-stdio-backend
    de-duplication actually happens: engram-search-praxis (etc.) is
    spawned ONCE and reused across every project that references the same
    shared_key, since it's a stateless, family-wide code index unlike
    per-repo-bound serena."""

    def test_http_spec_becomes_http_relay_adapter(self, engram_gateway):
        registry = {"kubernaut": {"docs": {"kind": "http", "url": "http://x/mcp/kubernaut-docs/"}}}

        adapters = engram_gateway.build_backend_adapters(registry)

        assert isinstance(adapters["kubernaut"]["docs"], engram_gateway.HttpRelayAdapter)
        assert adapters["kubernaut"]["docs"].url == "http://x/mcp/kubernaut-docs/"

    def test_stdio_spec_without_shared_key_becomes_its_own_adapter(self, engram_gateway):
        registry = {
            "dcm-cli": {"serena": {"kind": "stdio", "command": "uvx", "args": ["a"], "env": None}},
            "dcm-utilities": {"serena": {"kind": "stdio", "command": "uvx", "args": ["b"], "env": None}},
        }

        adapters = engram_gateway.build_backend_adapters(registry)

        assert adapters["dcm-cli"]["serena"] is not adapters["dcm-utilities"]["serena"]
        assert adapters["dcm-cli"]["serena"].args == ["a"]
        assert adapters["dcm-utilities"]["serena"].args == ["b"]

    def test_stdio_specs_sharing_a_shared_key_get_the_same_adapter_instance(self, engram_gateway):
        registry = {
            "praxis-grid": {"code": {"kind": "stdio", "command": "x", "args": [], "env": None, "shared_key": "praxis-code"}},
            "praxis-ai": {"code": {"kind": "stdio", "command": "x", "args": [], "env": None, "shared_key": "praxis-code"}},
        }

        adapters = engram_gateway.build_backend_adapters(registry)

        assert adapters["praxis-grid"]["code"] is adapters["praxis-ai"]["code"]

    def test_different_shared_keys_get_different_adapter_instances(self, engram_gateway):
        registry = {
            "praxis-grid": {"code": {"kind": "stdio", "command": "x", "args": [], "env": None, "shared_key": "praxis-code"}},
            "dcm-cli": {"code": {"kind": "stdio", "command": "y", "args": [], "env": None, "shared_key": "dcm-code"}},
        }

        adapters = engram_gateway.build_backend_adapters(registry)

        assert adapters["praxis-grid"]["code"] is not adapters["dcm-cli"]["code"]


class TestBuildApp:
    def test_mounts_one_route_per_requested_project(self, engram_gateway):
        app = engram_gateway.build_app(projects={"praxis-grid": {}})

        paths = {route.path for route in app.routes}
        assert paths == {"/mcp/praxis-grid"}

    def test_get_requests_are_declined_with_405(self, engram_gateway):
        from starlette.testclient import TestClient

        app = engram_gateway.build_app(projects={"praxis-grid": {}})
        client = TestClient(app)

        response = client.get("/mcp/praxis-grid")

        assert response.status_code == 405
