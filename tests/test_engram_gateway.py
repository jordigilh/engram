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

import pytest


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

    def test_kuadrant_docs_and_issues_get_project_qualified_names(self, engram_gateway):
        """kuadrant is cross-mounted as a second MCP server into every
        praxis-* repo -- a bare docs_recall/issues_recall would collide
        with that repo's own "engram" mount, so these get a longer,
        project-qualified prefix instead of the usual docs_/issues_."""
        assert engram_gateway.prefixed_tool_name("kuadrant_docs", "recall") == "kuadrant_docs_recall"
        assert engram_gateway.prefixed_tool_name("kuadrant_issues", "recall") == "kuadrant_issues_recall"


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


class TestFilterRelevantTools:
    """Covers the tool-count-ceiling fix (docs/findings/2026-08.md,
    2026-08-22 "MCP shows Disabled" entry): hindsight-shaped and serena
    backends get trimmed to their actually-used subset before ever reaching
    `build_catalog`, so a repo wired to multiple heavy backends can't blow
    past Cursor's active-tool ceiling and get stuck `Disabled`."""

    def test_hindsight_backend_keeps_only_relevant_tools(self, engram_gateway):
        tools = [_tool("recall"), _tool("retain"), _tool("delete_bank"), _tool("clear_memories")]

        filtered = engram_gateway.filter_relevant_tools("docs", tools)

        assert {t["name"] for t in filtered} == {"recall", "retain"}

    def test_hindsight_backend_keeps_create_mental_model(self, engram_gateway):
        """2026-08-26: project teams need self-serve mental model creation
        (previously only possible via this repo's own admin-side
        `engram.maintenance.create_mental_models` script), so this tool was
        added back to the relevant set alongside the pre-existing
        get/list/refresh trio -- update/delete/clear remain filtered out."""
        tools = [_tool("create_mental_model"), _tool("update_mental_model"), _tool("delete_mental_model")]

        filtered = engram_gateway.filter_relevant_tools("docs", tools)

        assert {t["name"] for t in filtered} == {"create_mental_model"}

    def test_issues_backend_uses_the_same_relevant_set_as_docs(self, engram_gateway):
        tools = [_tool("reflect"), _tool("list_directives")]

        filtered = engram_gateway.filter_relevant_tools("issues", tools)

        assert {t["name"] for t in filtered} == {"reflect"}

    def test_serena_backend_keeps_only_relevant_tools(self, engram_gateway):
        tools = [_tool("find_symbol"), _tool("write_memory"), _tool("open_dashboard")]

        filtered = engram_gateway.filter_relevant_tools("serena", tools)

        assert {t["name"] for t in filtered} == {"find_symbol"}

    def test_unfiltered_backend_passes_through_unchanged(self, engram_gateway):
        tools = [_tool("praxis_code_search")]

        filtered = engram_gateway.filter_relevant_tools("code", tools)

        assert filtered == tools

    def test_empty_tool_list_stays_empty(self, engram_gateway):
        assert engram_gateway.filter_relevant_tools("docs", []) == []

    def test_kuadrant_docs_and_issues_are_recall_only(self, engram_gateway):
        """2026-08-27: kuadrant is prior-art reference material cross-mounted
        into every praxis-* repo -- retain/reflect/mental-model management
        stay filtered out even though they're kept for kuadrant's own
        "docs"/"issues" keys used elsewhere; nobody retains into Kuadrant's
        banks from a praxis window."""
        tools = [_tool("recall"), _tool("retain"), _tool("reflect"), _tool("create_mental_model")]

        assert {t["name"] for t in engram_gateway.filter_relevant_tools("kuadrant_docs", tools)} == {"recall"}
        assert {t["name"] for t in engram_gateway.filter_relevant_tools("kuadrant_issues", tools)} == {"recall"}

    def test_kuadrant_code_keeps_only_search(self, engram_gateway):
        tools = [_tool("kuadrant_code_search"), _tool("kuadrant_code_pattern_search"), _tool("kuadrant_call_graph_blast_radius")]

        filtered = engram_gateway.filter_relevant_tools("kuadrant_code", tools)

        assert {t["name"] for t in filtered} == {"kuadrant_code_search"}


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

    def test_drops_irrelevant_tools_from_oversized_backends_before_aggregating(self, engram_gateway):
        """The exact kubernaut-family shape that triggered the ceiling bug:
        docs+issues+serena combined raw catalogs way over budget, trimmed
        down to the actually-used subset in the final aggregated list."""
        backends = {
            "docs": FakeAdapter(tools=[_tool("recall"), _tool("delete_bank")]),
            "issues": FakeAdapter(tools=[_tool("retain"), _tool("list_directives")]),
            "serena": FakeAdapter(tools=[_tool("find_symbol"), _tool("write_memory")]),
        }

        tool_defs, catalog, _errors = asyncio.run(engram_gateway.aggregate_tools_list(backends))

        names = {t["name"] for t in tool_defs}
        assert names == {"docs_recall", "issues_retain", "find_symbol"}
        assert "docs_delete_bank" not in catalog
        assert "issues_list_directives" not in catalog
        assert "write_memory" not in catalog

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
    everything on shared HTTP daemons (as of 2026-08-25, kubernaut-console
    included -- it was the one family member missing a serena entry despite
    already being a registered project on the shared daemon, see
    test_kubernaut_console_has_serena_scoped_to_its_own_repo below); koku-
    family's cocoindex-code is still stdio while its docs/issues/serena are
    HTTP; dcm/praxis/rhdh-plugins/koku-insights-onprem run cocoindex-code and
    serena as per-family-shared / per-repo stdio respectively; kubernaut-docs
    has no cocoindex-code and engram itself has neither issues nor serena --
    the registry must reflect exactly what each repo already has today, never
    add a backend that isn't already configured for it."""

    def test_covers_every_onboarded_project(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        assert len(registry) == 35

    def test_kubernaut_family_is_fully_http_already(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        spec = registry["kubernaut-operator"]
        assert spec["docs"] == {"kind": "http", "url": "http://localhost:8888/mcp/kubernaut-docs/"}
        assert spec["issues"] == {"kind": "http", "url": "http://localhost:8888/mcp/kubernaut-issues/"}
        assert spec["code"] == {"kind": "http", "url": "http://127.0.0.1:8891/mcp"}
        assert spec["serena"] == {"kind": "http", "url": "http://127.0.0.1:8893/mcp/kubernaut-operator"}

    def test_kubernaut_console_has_serena_scoped_to_its_own_repo(self, engram_gateway):
        """Added 2026-08-25: kubernaut-console was the only kubernaut-family
        repo with no serena entry, even though serena_multiplex.py's
        KUBERNAUT_FAMILY_PROJECTS already lists "kubernaut-console" as a
        registered project on the shared daemon. Its mount pins
        activate_project to "kubernaut-console" for scoped tool calls
        (find_symbol, replace_symbol_body, ...), while query_project/
        list_queryable_projects stay project-agnostic (see
        serena_multiplex.py's PROJECT_AGNOSTIC_TOOLS) and forward untouched --
        so this same mount also gives kubernaut-console read-only lookups
        into kubernaut/kubernaut-operator without any extra registry entry."""
        registry = engram_gateway.build_project_registry("/home/u")

        assert set(registry["kubernaut-console"]) == {"docs", "issues", "code", "serena"}
        assert registry["kubernaut-console"]["serena"] == {
            "kind": "http",
            "url": "http://127.0.0.1:8893/mcp/kubernaut-console",
        }

    def test_kubernaut_console_code_backend_is_shared_kubernaut_http_daemon(self, engram_gateway):
        """Regression guard: kubernaut-console's "code" backend previously
        pointed at a flat `~/.hindsight/cocoindex-search.py` stdio script that
        the 2026-08-12 package restructuring (src/engram/search/kubernaut.py +
        console-script rename) had already deleted 9 days before this
        registry entry was even authored -- so it was dead on arrival, and
        (being single-repo/no-args, unlike engram-search-kubernaut) would only
        ever have searched kubernaut-console's own code even if it had run,
        never kubernaut/kubernaut-operator upstream. The shared
        :8891 daemon (`engram-search-kubernaut` / src/engram/search/
        kubernaut.py) already indexes all three repos into one
        cocoindex.code_embeddings table and defaults `cocoindex_search` to
        whole-platform results -- the same daemon kubernaut/kubernaut-operator
        already use -- so pointing kubernaut-console at it too is what actually
        gives it operator + kubernaut-upstream code search."""
        registry = engram_gateway.build_project_registry("/home/u")

        assert registry["kubernaut-console"]["code"] == {"kind": "http", "url": "http://127.0.0.1:8891/mcp"}

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

    def test_kuadrant_is_one_entry_not_one_per_repo(self, engram_gateway):
        """2026-08-27: unlike every other family, Kuadrant's 8 repos are
        ingestion-only prior-art (nobody opens them as a Cursor workspace),
        so they collapse into a single "kuadrant" registry entry meant to
        be cross-mounted into praxis-* repos, not 8 per-repo entries."""
        registry = engram_gateway.build_project_registry("/home/u")

        assert "kuadrant" in registry
        for repo in ("kuadrant-operator", "limitador", "wasm-shim", "architecture", "authorino"):
            assert repo not in registry

    def test_kuadrant_has_no_serena(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        assert set(registry["kuadrant"]) == {"kuadrant_docs", "kuadrant_issues", "kuadrant_code"}

    def test_kuadrant_backends_point_at_their_own_banks_and_search_server(self, engram_gateway):
        registry = engram_gateway.build_project_registry("/home/u")

        spec = registry["kuadrant"]
        assert spec["kuadrant_docs"] == {"kind": "http", "url": "http://localhost:8888/mcp/kuadrant-docs/"}
        assert spec["kuadrant_issues"] == {"kind": "http", "url": "http://localhost:8888/mcp/kuadrant-issues/"}
        assert spec["kuadrant_code"]["kind"] == "stdio"
        assert spec["kuadrant_code"]["command"] == "/home/u/.hindsight/venv/bin/engram-search-kuadrant"


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


class TestStdioSubprocessAdapterCallToolSerialization:
    """2026-08-25: koku team hit a consistent (not transient) failure where
    every call_tool through this adapter -- koku_code_search, find_symbol,
    etc. -- failed client-side with a generic "backend is currently down"
    even though the backend executed successfully and returned real data.
    Root cause: `content.model_dump()` (no exclude_none) serializes the mcp
    SDK's optional `annotations`/`meta` fields as explicit `null` rather
    than omitting them, and MCP clients that validate content blocks
    against the schema (which requires `annotations` to be a real object or
    absent, never `null`) reject the whole result before the caller ever
    sees the data."""

    def test_call_tool_omits_none_valued_optional_fields_from_content(self, engram_gateway):
        from mcp.types import TextContent

        adapter = engram_gateway.StdioSubprocessAdapter(command="cmd", args=[])

        class _FakeResult:
            content = [TextContent(type="text", text="hello")]
            is_error = False

        class _FakeSession:
            async def call_tool(self, name, arguments):
                return _FakeResult()

        adapter._session = _FakeSession()  # bypasses _ensure_started's subprocess spawn

        result = asyncio.run(adapter.call_tool("some_tool", {}))

        assert result["content"] == [{"type": "text", "text": "hello"}]
        assert "annotations" not in result["content"][0]
        assert "meta" not in result["content"][0]

    def test_call_tool_reads_is_error_not_camelcase_isError(self, engram_gateway):
        """2026-08-27: mcp==2.0.0 (2026-08-22 dependabot bump) renamed
        CallToolResult.isError -> is_error, the same rename pattern that
        already hit Tool.inputSchema -> input_schema. Silent regression:
        `result.isError` on the real SDK object raised AttributeError,
        surfacing every kuadrant_code_search (and any other stdio backend)
        call as a generic "backend failed" error instead of real results."""
        from mcp.types import TextContent

        adapter = engram_gateway.StdioSubprocessAdapter(command="cmd", args=[])

        class _FakeResult:
            content = [TextContent(type="text", text="hello")]
            is_error = True

        class _FakeSession:
            async def call_tool(self, name, arguments):
                return _FakeResult()

        adapter._session = _FakeSession()

        result = asyncio.run(adapter.call_tool("some_tool", {}))

        assert result["isError"] is True


class TestStdioSubprocessAdapterListToolsSelfHeal:
    """2026-08-27: dcm's osac-service-provider Serena subprocess died hours
    into a live gateway session (unlike the 2026-08-25 incidents, this
    wasn't a cold-start race -- the process had already started
    successfully once, then exited later). `_ensure_started()` no-ops once
    `self._session` is set, so every subsequent `list_tools()` call kept
    hitting the same dead session and failing identically, silently
    dropping all 14 serena tools from the aggregated catalog on every
    `tools/list` call for hours with no self-heal -- unlike `call_tool()`,
    which already retries once via `_restart()`."""

    def test_list_tools_restarts_once_after_a_dead_session_and_succeeds(self, engram_gateway):
        from mcp.types import Tool

        adapter = engram_gateway.StdioSubprocessAdapter(command="cmd", args=[])

        class _DeadSession:
            async def list_tools(self):
                raise RuntimeError("write to closed pipe")

        class _FreshSession:
            async def list_tools(self):
                class _Result:
                    tools = [Tool(name="find_symbol", description="d", inputSchema={"type": "object"})]

                return _Result()

        adapter._session = _DeadSession()

        async def _fake_restart():
            adapter._session = _FreshSession()

        adapter._restart = _fake_restart

        result = asyncio.run(adapter.list_tools())

        assert [t["name"] for t in result] == ["find_symbol"]

    def test_list_tools_propagates_error_if_restart_also_fails(self, engram_gateway):
        """Must not swallow a genuinely broken backend -- one retry only,
        same philosophy as call_tool()."""
        adapter = engram_gateway.StdioSubprocessAdapter(command="cmd", args=[])

        class _AlwaysDeadSession:
            async def list_tools(self):
                raise RuntimeError("still dead")

        adapter._session = _AlwaysDeadSession()

        async def _fake_restart():
            adapter._session = _AlwaysDeadSession()

        adapter._restart = _fake_restart

        with pytest.raises(RuntimeError, match="still dead"):
            asyncio.run(adapter.list_tools())


class TestPrewarmStdioBackends:
    """2026-08-25: praxis-grid got stuck showing 16 tools after a gateway
    restart, even after the user restarted Cursor and disabled/re-enabled
    the MCP server. Root cause: Cursor calls tools/list once per session and
    caches the result, with no re-fetch trigger from this gateway -- so if
    that one call races a cold Serena/cocoindex LSP startup, the client is
    stuck with the degraded snapshot until a reconnect whose tools/list call
    happens to land after the backend is already warm. Pre-warming at
    startup closes that race window for the common case (a client connects
    more than a few seconds after the gateway process starts)."""

    def test_prewarms_every_stdio_backend_across_all_projects(self, engram_gateway):
        stdio_a = engram_gateway.StdioSubprocessAdapter(command="cmd-a", args=[])
        stdio_b = engram_gateway.StdioSubprocessAdapter(command="cmd-b", args=[])
        http_like = FakeAdapter(tools=[_tool("x")])  # not a StdioSubprocessAdapter -- must be skipped

        calls: list[str] = []

        async def _fake_list_a():
            calls.append("a")
            return []

        async def _fake_list_b():
            calls.append("b")
            return []

        stdio_a.list_tools = _fake_list_a
        stdio_b.list_tools = _fake_list_b

        projects = {
            "proj1": {"serena": stdio_a, "docs": http_like},
            "proj2": {"cocoindex-code": stdio_b},
        }

        asyncio.run(engram_gateway._prewarm_stdio_backends(projects))

        assert sorted(calls) == ["a", "b"]
        assert http_like.list_calls == 0

    def test_prewarm_failure_for_one_backend_does_not_raise(self, engram_gateway):
        stdio_ok = engram_gateway.StdioSubprocessAdapter(command="cmd-ok", args=[])
        stdio_broken = engram_gateway.StdioSubprocessAdapter(command="cmd-broken", args=[])

        ok_calls: list[str] = []

        async def _fake_list_ok():
            ok_calls.append("ok")
            return []

        async def _fake_list_broken():
            raise RuntimeError("cold LSP startup timed out")

        stdio_ok.list_tools = _fake_list_ok
        stdio_broken.list_tools = _fake_list_broken

        projects = {"proj": {"good": stdio_ok, "bad": stdio_broken}}

        # Must not raise -- a slow/broken backend shouldn't take down
        # pre-warming (or the gateway) for every other project's backends.
        asyncio.run(engram_gateway._prewarm_stdio_backends(projects))

        assert ok_calls == ["ok"]

    def test_build_app_schedules_prewarm_on_startup_without_blocking(self, engram_gateway):
        import time

        from starlette.testclient import TestClient

        stdio = engram_gateway.StdioSubprocessAdapter(command="cmd", args=[])
        warmed = {"done": False}

        async def _fake_list_tools():
            warmed["done"] = True
            return []

        stdio.list_tools = _fake_list_tools

        app = engram_gateway.build_app(projects={"proj": {"serena": stdio}})

        with TestClient(app) as client:
            # Lifespan startup completes synchronously (TestClient waits for
            # it), but the pre-warm task itself is merely *scheduled* there,
            # not awaited to completion -- poll briefly for it to run.
            deadline = time.monotonic() + 2.0
            while not warmed["done"] and time.monotonic() < deadline:
                time.sleep(0.01)
            assert warmed["done"]
            # And normal request handling still works while/after this runs.
            response = client.get("/mcp/proj")
            assert response.status_code == 405


class TestPrewarmProjectCatalogs:
    """2026-08-27: kubernaut's client saw `initialize` succeed ("namespace
    ready") but every `tools/call` fail with "Unknown tool ... backend is
    currently down" -- caused by `state[project]["catalog"]` starting as
    `{}` and staying empty until *some* client sends that project's
    `tools/list` in this process's lifetime. `_prewarm_stdio_backends`
    (above) only warms each stdio subprocess's connection, never touches
    `state`, so it didn't close this gap for a client whose session
    predates a gateway restart and never re-sends `tools/list` on its own."""

    def test_prewarms_catalog_for_every_project(self, engram_gateway):
        backend_a = FakeAdapter(tools=[_tool("recall")])
        backend_b = FakeAdapter(tools=[_tool("cocoindex_search")])
        projects = {
            "proj1": {"docs": backend_a},
            "proj2": {"code": backend_b},
        }
        state = {name: {"catalog": {}, "backends": backends} for name, backends in projects.items()}

        asyncio.run(engram_gateway._prewarm_project_catalogs(projects, state))

        assert "docs_recall" in state["proj1"]["catalog"]
        assert "cocoindex_search" in state["proj2"]["catalog"]

    def test_tools_call_works_immediately_after_prewarm_with_no_prior_tools_list(self, engram_gateway):
        """Direct regression test for the reported symptom: a tools/call for
        a project that has never had tools/list called against it in this
        process must still succeed once _prewarm_project_catalogs has run,
        instead of route_call() finding an empty catalog and returning the
        "Unknown tool ... backend is currently down" error."""
        backend = FakeAdapter(tools=[_tool("recall")], call_results={"recall": {"content": [], "isError": False}})
        projects = {"kubernaut": {"docs": backend}}
        state = {name: {"catalog": {}, "backends": backends} for name, backends in projects.items()}

        asyncio.run(engram_gateway._prewarm_project_catalogs(projects, state))

        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "docs_recall", "arguments": {}}}
        result = asyncio.run(engram_gateway.handle_tools_call(message, state["kubernaut"]["catalog"], state["kubernaut"]["backends"]))

        assert result["result"]["isError"] is False

    def test_prewarm_failure_for_one_project_does_not_raise_or_block_others(self, engram_gateway):
        good = FakeAdapter(tools=[_tool("recall")])
        broken = FakeAdapter(list_error=RuntimeError("backend down"))
        projects = {"good_proj": {"docs": good}, "bad_proj": {"docs": broken}}
        state = {name: {"catalog": {}, "backends": backends} for name, backends in projects.items()}

        asyncio.run(engram_gateway._prewarm_project_catalogs(projects, state))

        assert "docs_recall" in state["good_proj"]["catalog"]
        assert state["bad_proj"]["catalog"] == {}

    def test_build_app_schedules_catalog_prewarm_on_startup(self, engram_gateway):
        import time

        from starlette.testclient import TestClient

        backend = FakeAdapter(tools=[_tool("recall")])
        app = engram_gateway.build_app(projects={"proj": {"docs": backend}})

        with TestClient(app):
            deadline = time.monotonic() + 2.0
            while backend.list_calls == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert backend.list_calls > 0
