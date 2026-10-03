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
import json
import pathlib
import subprocess

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


def test_parse_sse_json_skips_empty_data_prelude(engram_gateway):
    result = engram_gateway._parse_sse_json(
        b'data: \nid: 0\nretry: 3000\n\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n'
    )

    assert result == {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}


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

    def test_manual_bank_tool_avoids_repeating_the_manual_prefix(self, engram_gateway):
        assert engram_gateway.prefixed_tool_name("docs_manual", "manual_mental_model") == "docs_manual_mental_model"


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

    def test_duplicate_unprefixed_name_across_backends_is_qualified(self, engram_gateway):
        """code and serena are both unprefixed -- if they ever define the
        same tool name (shouldn't happen today, verified empirically in the
        plan, but must fail safe rather than silently overwrite/crash), the
        second backend gets a qualified name."""
        catalog, tool_defs = engram_gateway.build_catalog(
            {"code": [_tool("shared_name")], "serena": [_tool("shared_name")]}
        )

        assert catalog == {
            "shared_name": ("code", "shared_name"),
            "serena_shared_name": ("serena", "shared_name"),
        }
        assert {tool["name"] for tool in tool_defs} == {"shared_name", "serena_shared_name"}


class TestRouteCall:
    def test_known_prefixed_tool_routes_and_strips_prefix(self, engram_gateway):
        catalog = {"docs_recall": ("docs", "recall")}

        assert engram_gateway.route_call("docs_recall", catalog) == ("docs", "recall")

    def test_known_unprefixed_tool_routes_with_unchanged_name(self, engram_gateway):
        catalog = {"find_symbol": ("serena", "find_symbol")}

        assert engram_gateway.route_call("find_symbol", catalog) == ("serena", "find_symbol")

    def test_unknown_tool_returns_none(self, engram_gateway):
        assert engram_gateway.route_call("nonexistent_tool", {}) is None


class TestExceptionFormatting:
    def test_empty_exception_keeps_type_and_explicit_no_message_marker(self, engram_gateway):
        detail = engram_gateway._format_exception(RuntimeError())

        assert detail == "RuntimeError: <no message>"

    def test_exception_chain_keeps_cause_type_and_message(self, engram_gateway):
        try:
            raise ValueError("upstream socket closed")
        except ValueError:
            try:
                raise RuntimeError()
            except RuntimeError as exc:
                detail = engram_gateway._format_exception(exc)

        assert "RuntimeError: <no message>" in detail
        assert "context ValueError: upstream socket closed" in detail


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

    def test_serena_pattern_search_description_guides_toward_symbol_tools(self, engram_gateway):
        original_description = "Search project files with a regular expression."
        tools = [
            _tool("search_for_pattern", original_description),
            _tool("find_symbol", "Find symbols by name."),
        ]

        filtered = engram_gateway.filter_relevant_tools("serena", tools)
        descriptions = {tool["name"]: tool["description"] for tool in filtered}

        pattern_description = descriptions["search_for_pattern"]
        assert pattern_description.startswith(original_description)
        assert "find_symbol" in pattern_description
        assert "find_referencing_symbols" in pattern_description
        assert "regex" in pattern_description.lower()
        assert "multiline" in pattern_description.lower()
        assert tools[0]["description"] == original_description

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

    def test_manual_mental_model_backend_keeps_only_manual_writer(self, engram_gateway):
        tools = [_tool("manual_mental_model"), _tool("refresh_mental_model")]

        filtered = engram_gateway.filter_relevant_tools("docs_manual", tools)

        assert {t["name"] for t in filtered} == {"manual_mental_model"}


class TestManualMentalModelAdapter:
    def test_lists_a_gateway_owned_manual_tool(self, engram_gateway):
        adapter = engram_gateway.ManualMentalModelAdapter(FakeAdapter())

        tools = asyncio.run(adapter.list_tools())

        assert [tool["name"] for tool in tools] == ["manual_mental_model"]
        assert tools[0]["inputSchema"]["required"] == ["model_id", "content"]

    def test_stores_a_replaceable_canonical_document_via_retain(self, engram_gateway):
        docs = FakeAdapter(call_results={"retain": {"content": [{"type": "text", "text": "accepted"}], "isError": False}})
        adapter = engram_gateway.ManualMentalModelAdapter(docs)

        result = asyncio.run(
            adapter.call_tool(
                "manual_mental_model",
                {
                    "model_id": "engram-architecture",
                    "content": "# Engram\n\nThe manual model.",
                    "name": "Engram Architecture",
                    "source_query": "How does Engram work?",
                    "tags": ["engram"],
                    "metadata": {"reviewer": "manual"},
                },
            )
        )

        assert result["isError"] is False
        assert docs.call_log == [
            (
                "retain",
                {
                    "content": "# Engram\n\nThe manual model.",
                    "context": "mental-models",
                    "document_id": "manual-mental-model-engram-architecture",
                    "metadata": {
                        "reviewer": "manual",
                        "source": "manual_mental_model",
                        "mental_model_id": "engram-architecture",
                        "name": "Engram Architecture",
                        "source_query": "How does Engram work?",
                    },
                    "update_mode": "replace",
                    "tags": ["engram"],
                },
            )
        ]

    def test_invalid_manual_content_is_rejected_without_backend_call(self, engram_gateway):
        docs = FakeAdapter()
        adapter = engram_gateway.ManualMentalModelAdapter(docs)

        result = asyncio.run(adapter.call_tool("manual_mental_model", {"model_id": "Bad ID", "content": "  "}))

        assert result["isError"] is True
        assert docs.call_log == []


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

    def test_cocoindex_graph_tools_are_exposed_unprefixed(self, engram_gateway):
        graph_tools = [
            "cocoindex_call_graph_blast_radius",
            "cocoindex_call_graph_shortest_path",
            "cocoindex_call_graph_get_cluster",
        ]
        code = FakeAdapter(tools=[_tool(name) for name in graph_tools])

        tool_defs, catalog, errors = asyncio.run(
            engram_gateway.aggregate_tools_list({"code": code})
        )

        assert {tool["name"] for tool in tool_defs} == set(graph_tools)
        assert {name: catalog[name] for name in graph_tools} == {
            name: ("code", name) for name in graph_tools
        }
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
            "code": FakeAdapter(list_error=RuntimeError("configured code search crashed")),
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

    def test_normalizes_prefixed_recall_from_legacy_endpoint_backend(self, engram_gateway):
        payload = {"results": [{"id": "memory-1", "text": "kept", "tags": ["engram"]}]}
        host = FakeAdapter(
            call_results={
                "docs_recall": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "isError": False,
                }
            }
        )
        catalog = {"docs_recall": ("host", "docs_recall")}
        message = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {"query": "x"}},
        }

        result = asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"host": host}))

        assert result["result"]["structuredContent"]["schema_version"] == "engram-recall.v1"
        assert result["result"]["structuredContent"]["results"] == [
            {"id": "memory-1", "tags": ["engram"], "summary": "kept"}
        ]

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

    def test_empty_backend_exception_returns_diagnostic_type(self, engram_gateway):
        dead = FakeAdapter(call_error=RuntimeError())
        catalog = {"docs_recall": ("host", "docs_recall")}
        message = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {"query": "foo"}},
        }

        result = asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"host": dead}))

        text = result["result"]["content"][0]["text"]
        assert "backend 'host' failed" in text
        assert "RuntimeError: <no message>" in text

    def test_non_tools_call_method_returns_none_so_caller_can_forward_generically(self, engram_gateway):
        message = {"jsonrpc": "2.0", "method": "notifications/initialized"}

        result = asyncio.run(engram_gateway.handle_tools_call(message, {}, {}))

        assert result is None


class TestHttpRelayAdapter:
    @staticmethod
    def _response(payload, *, status_code=200, session_id=None):
        import httpx

        headers = {"content-type": "application/json"}
        if session_id:
            headers["mcp-session-id"] = session_id
        return httpx.Response(
            status_code,
            headers=headers,
            content=json.dumps(payload).encode(),
            request=httpx.Request("POST", "http://upstream/mcp"),
        )

    def test_retries_recall_after_transient_transport_failure(self, engram_gateway, monkeypatch):
        import httpx

        monkeypatch.setattr(engram_gateway, "HTTP_RETRY_DELAY_S", 0)
        calls = []
        tool_call_count = 0

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, json, headers):
                nonlocal tool_call_count
                calls.append(json["method"])
                if json["method"] == "initialize":
                    return self._response(
                        {"jsonrpc": "2.0", "id": 1, "result": {}},
                        session_id=f"session-{calls.count('initialize')}",
                    )
                if json["method"] == "notifications/initialized":
                    return self._response({}, status_code=202)
                tool_call_count += 1
                if tool_call_count == 1:
                    raise httpx.ReadError(
                        "upstream disconnected",
                        request=httpx.Request("POST", url),
                    )
                return self._response(
                    {"jsonrpc": "2.0", "id": 2, "result": {"content": [], "isError": False}},
                )

            async def delete(self, url, headers):
                return self._response({}, status_code=200)

            _response = staticmethod(TestHttpRelayAdapter._response)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
        adapter = engram_gateway.HttpRelayAdapter("http://upstream/mcp")

        result = asyncio.run(adapter.call_tool("docs_recall", {"query": "x"}))

        assert result == {"content": [], "isError": False}
        assert tool_call_count == 2
        assert calls.count("initialize") == 2

    def test_does_not_retry_non_idempotent_write_after_transport_failure(self, engram_gateway, monkeypatch):
        import httpx

        monkeypatch.setattr(engram_gateway, "HTTP_RETRY_DELAY_S", 0)
        calls = []

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, json, headers):
                calls.append(json["method"])
                if json["method"] == "initialize":
                    return httpx.Response(
                        200,
                        headers={"mcp-session-id": "session-1"},
                        content=b'{"jsonrpc":"2.0","id":1,"result":{}}',
                        request=httpx.Request("POST", url),
                    )
                if json["method"] == "notifications/initialized":
                    return httpx.Response(202, request=httpx.Request("POST", url))
                raise httpx.ReadError("upstream disconnected", request=httpx.Request("POST", url))

            async def delete(self, url, headers):
                return httpx.Response(200, request=httpx.Request("DELETE", url))

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
        adapter = engram_gateway.HttpRelayAdapter("http://upstream/mcp")

        with pytest.raises(httpx.ReadError):
            asyncio.run(adapter.call_tool("retain", {"content": "do not duplicate"}))

        assert calls == ["initialize", "notifications/initialized", "tools/call"]

    def test_tools_list_retries_transient_http_status(self, engram_gateway, monkeypatch):
        import httpx

        monkeypatch.setattr(engram_gateway, "HTTP_RETRY_DELAY_S", 0)
        initialize_count = 0

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, json, headers):
                nonlocal initialize_count
                if json["method"] == "initialize":
                    initialize_count += 1
                    return httpx.Response(
                        200,
                        headers={"mcp-session-id": f"session-{initialize_count}"},
                        content=b'{"jsonrpc":"2.0","id":1,"result":{}}',
                        request=httpx.Request("POST", url),
                    )
                if json["method"] == "notifications/initialized":
                    return httpx.Response(202, request=httpx.Request("POST", url))
                if initialize_count == 1:
                    return httpx.Response(503, request=httpx.Request("POST", url))
                return httpx.Response(
                    200,
                    content=b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"recall"}]}}',
                    request=httpx.Request("POST", url),
                )

            async def delete(self, url, headers):
                return httpx.Response(200, request=httpx.Request("DELETE", url))

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
        adapter = engram_gateway.HttpRelayAdapter("http://upstream/mcp")

        assert asyncio.run(adapter.list_tools()) == [{"name": "recall"}]
        assert initialize_count == 2


class TestRecallResponseNormalization:
    def test_recall_becomes_bounded_readable_and_structured(self, engram_gateway):
        payload = {
            "results": [
                {
                    "id": f"memory-{index}",
                    "text": "fact " + ("x" * 1500),
                    "fact_type": "world",
                    "context": "project",
                    "document_id": "engram-doc",
                    "metadata": {"repo": "engram", "key_sentences": "y" * 1500, "keywords": "gateway, recall"},
                    "tags": ["engram"],
                    "scores": {"final": 0.123456, "semantic": None},
                }
                for index in range(10)
            ],
            "source_facts_truncated": None,
        }
        docs = FakeAdapter(
            call_results={
                "recall": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "isError": False,
                }
            }
        )
        catalog = {"docs_recall": ("docs", "recall")}
        message = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {"query": "x"}},
        }

        result = asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"docs": docs}))

        structured = result["result"]["structuredContent"]
        assert structured["schema_version"] == "engram-recall.v1"
        assert structured["total_results"] == 10
        assert structured["returned_results"] == 8
        assert structured["truncated"] is True
        assert len(structured["results"]) == 8
        assert structured["results"][0]["summary"].endswith("[truncated; 1500 chars total]")
        assert "text" not in structured["results"][0]
        assert structured["results"][0]["keywords"] == ["gateway", "recall"]
        assert structured["results"][0]["metadata"] == {"repo": "engram"}
        assert structured["results"][0]["scores"] == {"final": 0.1235}

        rendered = result["result"]["content"][0]["text"]
        assert rendered.startswith("Engram recall (8 of 10 results; schema engram-recall.v1)")
        assert "document=engram-doc" in rendered
        assert "2 lower-ranked results omitted" in rendered
        assert len(json.dumps(result["result"])) < 30_000

    def test_existing_structured_content_is_normalized_without_requiring_text(self, engram_gateway):
        docs = FakeAdapter(
            call_results={
                "recall": {
                    "content": [],
                    "isError": False,
                    "structuredContent": {
                        "results": [{"id": "memory-1", "text": "kept", "tags": ["engram"]}],
                    },
                }
            }
        )
        catalog = {"docs_recall": ("docs", "recall")}
        message = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "docs_recall"}}

        result = asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"docs": docs}))

        assert result["result"]["structuredContent"]["results"] == [
            {"id": "memory-1", "tags": ["engram"], "summary": "kept"}
        ]
        assert "kept" in result["result"]["content"][0]["text"]

    def test_recall_normalization_is_idempotent_for_source_summaries(self, engram_gateway):
        original = {
            "content": [{"type": "text", "text": '{"results":[{"id":"memory-1","text":"source text"}]}'}],
            "isError": False,
        }

        normalized = engram_gateway._normalize_recall_result(original)
        normalized_again = engram_gateway._normalize_recall_result(normalized)

        assert normalized_again == normalized
        assert normalized_again["structuredContent"]["results"][0]["summary"] == "source text"
        assert "source text" in normalized_again["content"][0]["text"]

    def test_non_recall_or_malformed_payload_passes_through(self, engram_gateway):
        original = {"content": [{"type": "text", "text": '{"items":[1,2]}'}], "isError": False}
        assert engram_gateway._normalize_recall_result(original) is original

        malformed = {"content": [{"type": "text", "text": "not JSON"}], "isError": False}
        assert engram_gateway._normalize_recall_result(malformed) is malformed


class TestSerenaPatternResponseNormalization:
    def test_oversized_search_returns_compact_grouped_partial_matches(self, engram_gateway):
        matches = {
            "test/services/mock-llm/scenarios/scenario_default_fallback.go": [
                {
                    "line": 21,
                    "text": 'ScenarioName: "default", SignalName: "Unknown", Severity: "warning",',
                },
                {
                    "line": 21,
                    "text": 'ScenarioName: "default", SignalName: "Unknown", Severity: "warning",',
                },
                {
                    "line": 22,
                    "text": 'WorkflowName: "generic-restart-v1", WorkflowID: uuid.DeterministicUUID("generic-restart-v1"),',
                },
            ],
            "test/services/mock-llm/scenarios/scenario_oomkilled.go": [
                {"line": 26, "text": 'WorkflowName: "oomkill-increase-memory-v1",'},
            ],
        }
        diagnostic = (
            'Substring pattern: WorkflowName:.*generic-restart|"Unknown"|ScenarioName: "default"\n'
            "Paths include glob: test/services/mock-llm/scenarios/**/*.go\n"
            "Restrict search to code files: Yes\n"
            "Context lines before: 1\n"
            "Context lines after: 3\n"
            "Max answer chars: 7000\n"
            "The answer is too long (20109 characters).\n"
            "Matched lines per file; use read_file with the line numbers for surrounding context:\n"
            + json.dumps(matches)
        )
        serena = FakeAdapter(
            call_results={
                "search_for_pattern": {
                    "content": [{"type": "text", "text": diagnostic}],
                    "isError": True,
                }
            }
        )
        message = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "search_for_pattern",
                "arguments": {"substring_pattern": "WorkflowName"},
            },
        }

        response = asyncio.run(
            engram_gateway.handle_tools_call(
                message,
                {"search_for_pattern": ("serena", "search_for_pattern")},
                {"serena": serena},
            )
        )["result"]

        structured = response["structuredContent"]
        assert response["isError"] is False
        assert structured["schema_version"] == "engram-serena-pattern-search.v1"
        assert structured["status"] == "partial"
        assert structured["answer_chars"] == 20109
        assert structured["max_answer_chars"] == 7000
        assert structured["matched_file_count"] == 2
        assert structured["matched_line_count"] == 3
        assert structured["matches_by_file"][0]["matches"][0]["line"] == 21
        assert len(response["content"][0]["text"]) < 7000
        assert "20,109" in response["content"][0]["text"]
        assert "scenario_default_fallback.go" in response["content"][0]["text"]
        assert "L22" in response["content"][0]["text"]

    def test_unrecognized_serena_errors_are_preserved(self, engram_gateway):
        original = {
            "content": [{"type": "text", "text": "backend unavailable"}],
            "isError": True,
        }

        assert engram_gateway._serena_pattern_result(original) is original


class TestEstimateTokens:
    """2026-08-30: user asked whether MCP call token consumption could be
    calculated. Cursor's own hooks (afterMCPExecution etc.) don't carry any
    token field -- confirmed against the hooks docs -- so the only viable
    local source is estimating from the response text itself, done here
    with tiktoken (already an installed transitive dep, pinned explicitly
    now) rather than a cruder chars/4 heuristic. This is necessarily an
    approximation: tiktoken's BPE encodings are OpenAI's, not Anthropic's
    exact tokenizer, since Anthropic doesn't publish one."""

    def test_empty_text_is_zero_tokens(self, engram_gateway):
        assert engram_gateway._estimate_tokens("") == 0

    def test_known_short_string_matches_expected_cl100k_count(self, engram_gateway):
        # Stable, well-known tiktoken fact: "hello world" is exactly 2
        # tokens under cl100k_base -- pins the encoding choice, not just
        # "returns some positive number".
        assert engram_gateway._estimate_tokens("hello world") == 2

    def test_longer_text_yields_a_larger_estimate(self, engram_gateway):
        short = engram_gateway._estimate_tokens("hello")
        long = engram_gateway._estimate_tokens("hello " * 200)
        assert long > short

    def test_tiktoken_failure_falls_back_to_chars_over_four_heuristic(self, engram_gateway, monkeypatch):
        def _boom():
            raise RuntimeError("no network / encoding download failed")

        monkeypatch.setattr(engram_gateway, "_tiktoken_encoding", _boom)

        # 12 chars // 4 == 3, the documented fallback -- not just "doesn't crash".
        assert engram_gateway._estimate_tokens("abcdefghijkl") == 3


class TestExtractResultText:
    def test_concatenates_text_from_all_content_items(self, engram_gateway):
        result = {"content": [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]}

        assert engram_gateway._extract_result_text(result) == "foobar"

    def test_ignores_non_text_content_items(self, engram_gateway):
        result = {"content": [{"type": "image", "data": "base64stuff"}, {"type": "text", "text": "caption"}]}

        assert engram_gateway._extract_result_text(result) == "caption"

    def test_missing_content_key_is_empty_string(self, engram_gateway):
        assert engram_gateway._extract_result_text({}) == ""

    def test_empty_content_list_is_empty_string(self, engram_gateway):
        assert engram_gateway._extract_result_text({"content": []}) == ""


class TestGatewayCallMetricsLogging:
    """`handle_tools_call` is the single choke point every MCP call passes
    through with the actual, un-redacted response content available (unlike
    Cursor's afterMCPExecution hook, whose result_chars is frequently 0 --
    see cursor/hooks/log-mcp-calls.sh's own comment) -- so this is where
    real per-call token estimates get logged, to a gateway-owned JSONL file
    distinct from the client-side hook's mcp-calls.jsonl."""

    def test_successful_call_logs_one_jsonl_line_with_expected_fields(self, engram_gateway, tmp_path, monkeypatch):
        log_path = tmp_path / "gateway-calls.jsonl"
        monkeypatch.setattr(engram_gateway, "GATEWAY_CALLS_LOG", log_path)

        docs = FakeAdapter(call_results={"recall": {"content": [{"type": "text", "text": "hello world"}], "isError": False}})
        catalog = {"docs_recall": ("docs", "recall")}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "docs_recall", "arguments": {}}}

        asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"docs": docs}, project="praxis-grid"))

        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["project"] == "praxis-grid"
        assert entry["backend"] == "docs"
        assert entry["tool"] == "docs_recall"
        assert entry["is_error"] is False
        assert entry["result_chars"] == len("hello world")
        assert entry["est_tokens"] == 2
        assert "output_chars" not in entry
        assert "savings_tokens" not in entry

    def test_successful_call_returns_backend_result_without_spike_shaping(self, engram_gateway, tmp_path, monkeypatch):
        log_path = tmp_path / "gateway-calls.jsonl"
        monkeypatch.setattr(engram_gateway, "GATEWAY_CALLS_LOG", log_path)

        original = '{ "items": [ 1, 2, 3 ], "note": "keep  spaces" }'
        docs = FakeAdapter(call_results={"recall": {"content": [{"type": "text", "text": original}], "isError": False}})
        catalog = {"docs_recall": ("docs", "recall")}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "docs_recall", "arguments": {}}}

        result = asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"docs": docs}, project="engram"))

        assert result["result"]["content"][0]["text"] == original
        entry = json.loads(log_path.read_text().strip())
        assert entry["result_chars"] == len(original)
        assert entry["est_tokens"] == engram_gateway._estimate_tokens(original)
        assert "shaping" not in entry

    def test_project_defaults_to_none_when_not_passed(self, engram_gateway, tmp_path, monkeypatch):
        """Existing call sites/tests predate the `project` param -- must stay
        backward compatible rather than requiring every caller to update."""
        log_path = tmp_path / "gateway-calls.jsonl"
        monkeypatch.setattr(engram_gateway, "GATEWAY_CALLS_LOG", log_path)

        docs = FakeAdapter(call_results={"recall": {"content": [], "isError": False}})
        catalog = {"docs_recall": ("docs", "recall")}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "docs_recall", "arguments": {}}}

        asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"docs": docs}))

        entry = json.loads(log_path.read_text().strip())
        assert entry["project"] is None

    def test_backend_exception_logs_a_zero_token_error_entry(self, engram_gateway, tmp_path, monkeypatch):
        log_path = tmp_path / "gateway-calls.jsonl"
        monkeypatch.setattr(engram_gateway, "GATEWAY_CALLS_LOG", log_path)

        dead = FakeAdapter(call_error=RuntimeError("subprocess exited"))
        catalog = {"praxis_code_search": ("code", "praxis_code_search")}
        message = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "praxis_code_search", "arguments": {}}}

        asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"code": dead}, project="praxis-grid"))

        entry = json.loads(log_path.read_text().strip())
        assert entry["is_error"] is True
        assert entry["result_chars"] == 0
        assert entry["est_tokens"] == 0
        assert entry["tool"] == "praxis_code_search"

    def test_unknown_tool_does_not_write_a_log_line(self, engram_gateway, tmp_path, monkeypatch):
        """No backend was ever actually called -- nothing to attribute
        tokens/chars to, so this stays a pure routing error like it was
        before this feature (see TestHandleToolsCall's own coverage of the
        unchanged JSON-RPC error shape)."""
        log_path = tmp_path / "gateway-calls.jsonl"
        monkeypatch.setattr(engram_gateway, "GATEWAY_CALLS_LOG", log_path)
        message = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "ghost_tool", "arguments": {}}}

        asyncio.run(engram_gateway.handle_tools_call(message, {}, {}))

        assert not log_path.exists()

    def test_logging_failure_does_not_break_the_call(self, engram_gateway, monkeypatch):
        """A read-only/missing log directory must degrade to a warning, not
        take down the actual tool call it's trying to observe."""
        monkeypatch.setattr(engram_gateway, "GATEWAY_CALLS_LOG", pathlib.Path("/nonexistent-dir/gateway-calls.jsonl"))

        docs = FakeAdapter(call_results={"recall": {"content": [{"type": "text", "text": "ok"}], "isError": False}})
        catalog = {"docs_recall": ("docs", "recall")}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "docs_recall", "arguments": {}}}

        result = asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"docs": docs}))

        assert result["result"]["content"][0]["text"] == "ok"

    def test_call_appends_rather_than_overwrites_across_multiple_calls(self, engram_gateway, tmp_path, monkeypatch):
        log_path = tmp_path / "gateway-calls.jsonl"
        monkeypatch.setattr(engram_gateway, "GATEWAY_CALLS_LOG", log_path)

        docs = FakeAdapter(call_results={"recall": {"content": [{"type": "text", "text": "one"}], "isError": False}})
        catalog = {"docs_recall": ("docs", "recall")}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "docs_recall", "arguments": {}}}

        asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"docs": docs}))
        asyncio.run(engram_gateway.handle_tools_call(message, catalog, {"docs": docs}))

        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 2


class TestBuildBackendAdapters:
    """Real adapter instantiation from registry specs -- still no I/O
    (adapters connect lazily), but this is where shared-stdio-backend
    de-duplication happens: one configured code backend is spawned ONCE and
    reused across every project that references the same shared_key, since it
    is a stateless, family-wide code index unlike per-repo-bound serena."""

    def test_http_spec_becomes_http_relay_adapter(self, engram_gateway):
        registry = {"kubernaut": {"docs": {"kind": "http", "url": "http://x/mcp/kubernaut-docs/"}}}

        adapters = engram_gateway.build_backend_adapters(registry)

        assert isinstance(adapters["kubernaut"]["docs"], engram_gateway.HttpRelayAdapter)
        assert adapters["kubernaut"]["docs"].url == "http://x/mcp/kubernaut-docs/"
        assert isinstance(adapters["kubernaut"]["docs_manual"], engram_gateway.ManualMentalModelAdapter)
        assert adapters["kubernaut"]["docs_manual"].hindsight_adapter is adapters["kubernaut"]["docs"]

    def test_shadow_http_builds_a_shared_zvec_primary_adapter(self, engram_gateway, tmp_path):
        spec = {
            "kind": "shadow_http",
            "url": "http://127.0.0.1:7999/mcp",
            "shadow_url": "http://127.0.0.1:8891/mcp",
            "timeout_seconds": 300.0,
            "shadow_timeout_seconds": 180.0,
            "shadow_log": str(tmp_path / "shadow.jsonl"),
            "shared_key": "kubernaut-zvec-shadow",
        }
        adapters = engram_gateway.build_backend_adapters({
            "kubernaut": {"code": spec},
            "kubernaut-operator": {"code": dict(spec)},
        })

        primary = adapters["kubernaut"]["code"]
        assert isinstance(primary, engram_gateway.ZvecShadowRelayAdapter)
        assert adapters["kubernaut-operator"]["code"] is primary
        assert primary.primary.url == spec["url"]
        assert primary.shadow.url == spec["shadow_url"]
        assert primary.log_path == pathlib.Path(spec["shadow_log"])

    def test_manual_tools_are_added_for_each_writable_hindsight_bank(self, engram_gateway):
        registry = {
            "demo": {
                "docs": {"kind": "http", "url": "http://x/mcp/demo-docs/"},
                "issues": {"kind": "http", "url": "http://x/mcp/demo-issues/"},
            }
        }

        adapters = engram_gateway.build_backend_adapters(registry)

        assert isinstance(adapters["demo"]["docs_manual"], engram_gateway.ManualMentalModelAdapter)
        assert isinstance(adapters["demo"]["issues_manual"], engram_gateway.ManualMentalModelAdapter)

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


class TestZvecShadowRelayAdapter:
    def test_workspace_metadata_reports_untracked_worktree_content(self, engram_gateway, tmp_path):
        subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
        (tmp_path / "untracked.go").write_text("package sample\n")

        metadata = engram_gateway._git_workspace_metadata(tmp_path)

        assert metadata["workspace"] == str(tmp_path)
        assert metadata["branch"]
        assert metadata["commit"] is None
        assert metadata["dirty"] is True

    def test_semantic_comparison_uses_cocoindex_as_reference_and_reports_file_drift(
        self, engram_gateway
    ):
        primary = {
            "content": [{
                "type": "text",
                "text": (
                    "#1 [group_coverage: Q1] matchedBy=fts+vector pkg/a.go:1-4\n"
                    "#2 [global_fill] matchedBy=fts pkg/b.go:8-9\n"
                ),
            }]
        }
        shadow = {
            "content": [{
                "type": "text",
                "text": (
                    "[1] kubernaut/pkg/a.go (score: 0.9)\n"
                    "[2] kubernaut/pkg/c.go (score: 0.8)\n"
                ),
            }]
        }

        comparison = engram_gateway._compare_shadow_responses(
            "zvec_grep_search", primary, shadow, "kubernaut"
        )

        assert comparison["reference_engine"] == "cocoindex"
        assert comparison["source_of_truth"] == "cocoindex"
        assert comparison["cocoindex_only_top_k"] == ["pkg/c.go"]
        assert comparison["zvec_only_top_k"] == ["pkg/b.go"]
        assert comparison["shared_file_rank_deltas"] == [{
            "path": "pkg/a.go",
            "cocoindex_rank": 1,
            "zvec_rank": 1,
            "zvec_minus_cocoindex": 0,
        }]
        assert comparison["file_order_matches_reference"] is False

    def test_graph_comparison_reports_cocoindex_reference_callers(self, engram_gateway):
        primary = {
            "structuredContent": {
                "callers_by_depth": [["pkg/a.go::caller"], ["pkg/b.go::parent"]],
                "unresolved_calls": 42,
                "total_calls": 100,
                "ambiguous_calls": 11,
            }
        }
        shadow = {
            "content": [{
                "type": "text",
                "text": (
                    "Blast radius for kubernaut/pkg/a.go::target:\n"
                    "  depth 1: kubernaut/pkg/a.go::caller\n"
                    "  depth 2: kubernaut/pkg/c.go::parent\n"
                    "\n(name-based resolution, no type info -- 35/90 calls in this repo could not be "
                    "resolved to a known definition, and 9 matched 2+ candidates and were dropped)"
                ),
            }]
        }

        comparison = engram_gateway._compare_shadow_responses(
            "zvec_grep_callgraph_blast_radius", primary, shadow, "kubernaut"
        )

        assert comparison["reference_engine"] == "cocoindex"
        assert comparison["source_of_truth"] == "cocoindex"
        assert comparison["cocoindex_only_by_depth"] == [[], ["pkg/c.go::parent"]]
        assert comparison["zvec_only_by_depth"] == [[], ["pkg/b.go::parent"]]
        assert comparison["callers_match_reference"] is False
        assert comparison["zvec_minus_cocoindex_resolution_counts"] == {
            "unresolved_calls": 7,
            "total_calls": 10,
            "ambiguous_calls": 2,
        }

    def test_primary_returns_before_shadow_and_logs_same_root_ranked_comparison(
        self, engram_gateway, tmp_path
    ):
        class BlockingShadow(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def call_tool(self, name, arguments):
                self.call_log.append((name, arguments))
                self.started.set()
                await self.release.wait()
                return {"content": [{"type": "text", "text": "cocoindex ranked chunks"}], "isError": False}

        async def scenario():
            root = tmp_path.resolve()
            primary_result = {
                "content": [{"type": "text", "text": "zvec ranked chunks"}],
                "structuredContent": {"items": [{"rank": 1, "path": "pkg/workflow.go"}]},
                "isError": False,
            }
            primary = FakeAdapter(call_results={"zvec_grep_search": primary_result})
            shadow = BlockingShadow()
            log_path = tmp_path / "zvec-cocoindex-shadow.jsonl"
            adapter = engram_gateway.ZvecShadowRelayAdapter(
                primary,
                shadow,
                log_path=log_path,
                shadow_timeout_seconds=1,
                source_roots={"kubernaut": root},
            )

            arguments = {"root": str(root), "query": "workflow selection membership", "limit": 7}
            result = await asyncio.wait_for(adapter.call_tool("zvec_grep_search", arguments), timeout=0.1)
            assert result is primary_result
            await asyncio.wait_for(shadow.started.wait(), timeout=0.2)
            assert not log_path.exists(), "the shadow has not been released or logged yet"

            shadow.release.set()
            await asyncio.gather(*tuple(adapter._shadow_tasks))
            entry = json.loads(log_path.read_text().strip())

            assert shadow.call_log == [(
                "cocoindex_search",
                {"query": "workflow selection membership", "limit": 7, "repo": "kubernaut", "branch": "main"},
            )]
            assert entry["schema_version"] == "zvec-cocoindex-shadow.v1"
            assert entry["branch"] is None
            assert entry["commit"] is None
            assert entry["workspace"] == str(root)
            assert entry["primary"]["response"] == primary_result
            assert entry["shadow"]["response"]["content"][0]["text"] == "cocoindex ranked chunks"
            assert entry["comparison"]["reference_engine"] == "cocoindex"
            assert entry["comparison"]["zvec_only_top_k"] == ["pkg/workflow.go"]

        asyncio.run(scenario())

    def test_graph_tools_map_to_cocoindex_with_root_branch_and_repo_scope(self, engram_gateway, tmp_path):
        root = tmp_path / "kubernaut-v1.5"
        root.mkdir()
        tool, arguments = engram_gateway._zvec_shadow_arguments(
            "zvec_grep_callgraph_blast_radius",
            {"root": str(root), "function": "Reconcile", "depth": 3},
            repo="kubernaut",
            root=root,
            branch="fix/123-branch",
        )

        assert tool == "cocoindex_call_graph_blast_radius"
        assert arguments == {
            "function": "Reconcile",
            "depth": 3,
            "repo": "kubernaut",
            "branch": "v1.5",
        }

    def test_shadow_failure_is_logged_without_changing_primary_result(self, engram_gateway, tmp_path):
        async def scenario():
            root = tmp_path.resolve()
            primary_result = {"content": [{"type": "text", "text": "primary"}], "isError": False}
            adapter = engram_gateway.ZvecShadowRelayAdapter(
                FakeAdapter(call_results={"zvec_grep_search": primary_result}),
                FakeAdapter(call_error=RuntimeError("CocoIndex unavailable")),
                log_path=tmp_path / "shadow.jsonl",
                shadow_timeout_seconds=0.1,
                source_roots={"kubernaut": root},
            )

            result = await adapter.call_tool(
                "zvec_grep_search", {"root": str(root), "query": "workflow"}
            )
            await asyncio.gather(*tuple(adapter._shadow_tasks))
            entry = json.loads((tmp_path / "shadow.jsonl").read_text().strip())
            assert result is primary_result
            assert entry["shadow"]["error"] == "RuntimeError: CocoIndex unavailable"

        asyncio.run(scenario())

    def test_typescript_worktree_does_not_call_the_go_only_cocoindex_graph(self, engram_gateway, tmp_path):
        async def scenario():
            root = tmp_path.resolve()
            primary_result = {"content": [{"type": "text", "text": "zvec graph"}], "isError": False}
            shadow = FakeAdapter()
            adapter = engram_gateway.ZvecShadowRelayAdapter(
                FakeAdapter(call_results={"zvec_grep_callgraph_cluster": primary_result}),
                shadow,
                log_path=tmp_path / "shadow.jsonl",
                shadow_timeout_seconds=0.1,
                source_roots={"typescript-project": root},
            )

            result = await adapter.call_tool(
                "zvec_grep_callgraph_cluster",
                {"root": str(root), "function": "resolveTheme"},
            )
            await asyncio.gather(*tuple(adapter._shadow_tasks))
            entry = json.loads((tmp_path / "shadow.jsonl").read_text().strip())
            assert result is primary_result
            assert shadow.call_log == []
            assert entry["shadow"]["skipped"] == "cocoindex_callgraph_not_configured_for_source"

        asyncio.run(scenario())


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


class TestLoadInstanceRegistry:
    def test_native_gateway_defaults_to_deployment_local_config(self, engram_gateway, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))

        assert engram_gateway.default_native_config_path() == tmp_path / ".engram" / "runtime" / "native-instances.toml"

    def test_generic_runtime_example_is_valid_for_the_registry_loader(self, engram_gateway):
        example = pathlib.Path(__file__).parents[1] / "docs" / "runtime-instances.toml.example"

        registry = engram_gateway.load_instance_registry(example)

        assert set(registry) == {"my-project"}
        assert set(registry["my-project"]) == {"docs", "issues", "code", "serena"}
        assert all(spec["kind"] == "http" for spec in registry["my-project"].values())
        assert registry["my-project"]["code"]["timeout_seconds"] == 180

    def test_native_runtime_example_is_valid_for_the_registry_loader(self, engram_gateway):
        example = pathlib.Path(__file__).parents[1] / "docs" / "native-instances.toml.example"

        registry = engram_gateway.load_instance_registry(example)

        assert set(registry) == {"my-project"}
        assert set(registry["my-project"]) == {"docs", "issues", "code", "serena"}
        assert registry["my-project"]["code"]["kind"] == "stdio"
        assert registry["my-project"]["serena"]["args"][-1] == "/path/to/my-project"

    def test_loads_http_host_adapter_instances(self, engram_gateway, tmp_path):
        config = tmp_path / "instances.toml"
        config.write_text(
            '[instances.kubernaut]\n'
            'endpoint = "http://host.containers.internal:9001/mcp/kubernaut"\n'
            "timeout_seconds = 180\n"
        )

        assert engram_gateway.load_instance_registry(config) == {
            "kubernaut": {
                "host": {
                    "kind": "http",
                    "url": "http://host.containers.internal:9001/mcp/kubernaut",
                    "timeout_seconds": 180,
                }
            }
        }
        adapter = engram_gateway.build_backend_adapters(
            engram_gateway.load_instance_registry(config)
        )["kubernaut"]["host"]
        assert adapter.timeout_seconds == 180

    def test_loads_multiple_direct_backends_per_instance(self, engram_gateway, tmp_path):
        config = tmp_path / "instances.toml"
        config.write_text(
            """
[instances.kubernaut.backends.docs]
kind = "http"
url = "http://host.containers.internal:8888/mcp/kubernaut-docs/"

[instances.kubernaut.backends.code]
kind = "http"
url = "http://host.containers.internal:8891/mcp"
headers = { Host = "localhost:8891" }
timeout_seconds = 180

[instances.kubernaut.backends.serena]
kind = "http"
url = "http://host.containers.internal:8893/mcp/kubernaut"

[instances.kubernaut.backends.rca]
kind = "http"
url = "http://host.containers.internal:8897/mcp"
"""
        )

        registry = engram_gateway.load_instance_registry(config)

        assert set(registry["kubernaut"]) == {"docs", "code", "serena", "rca"}
        assert registry["kubernaut"]["code"]["headers"] == {"Host": "localhost:8891"}
        assert registry["kubernaut"]["code"]["timeout_seconds"] == 180
        adapters = engram_gateway.build_backend_adapters(registry)
        assert adapters["kubernaut"]["code"].headers == {"Host": "localhost:8891"}
        assert adapters["kubernaut"]["code"].timeout_seconds == 180

    def test_loads_shadow_http_backend_with_independent_timeouts(self, engram_gateway, tmp_path):
        config = tmp_path / "instances.toml"
        config.write_text(
            f"""
[instances.kubernaut.backends.code]
kind = "shadow_http"
url = "http://127.0.0.1:7999/mcp"
shadow_url = "http://127.0.0.1:8891/mcp"
timeout_seconds = 300
shadow_timeout_seconds = 180
shadow_log = "{tmp_path / 'shadow.jsonl'}"
shared_key = "kubernaut-zvec-shadow"
source_roots = {{ code = "{tmp_path}" }}
callgraph_repos = ["code"]
"""
        )

        registry = engram_gateway.load_instance_registry(config)

        assert registry["kubernaut"]["code"] == {
            "kind": "shadow_http",
            "url": "http://127.0.0.1:7999/mcp",
            "shadow_url": "http://127.0.0.1:8891/mcp",
            "timeout_seconds": 300.0,
            "shadow_timeout_seconds": 180.0,
            "shadow_log": str(tmp_path / "shadow.jsonl"),
            "shared_key": "kubernaut-zvec-shadow",
            "source_roots": {"code": str(tmp_path)},
            "callgraph_repos": ["code"],
        }

    def test_loads_stdio_backend_with_environment_and_shared_key(self, engram_gateway, tmp_path):
        config = tmp_path / "instances.toml"
        config.write_text(
            """
[instances.praxis.backends.code]
kind = "stdio"
command = "/opt/engram/search"
args = ["--repo", "praxis-grid"]
env = { COCOINDEX_PG_URL = "postgresql://example/db" }
shared_key = "praxis-code"
"""
        )

        registry = engram_gateway.load_instance_registry(config)

        assert registry["praxis"]["code"] == {
            "kind": "stdio",
            "command": "/opt/engram/search",
            "args": ["--repo", "praxis-grid"],
            "env": {"COCOINDEX_PG_URL": "postgresql://example/db"},
            "shared_key": "praxis-code",
        }

    @pytest.mark.parametrize(
        "contents",
        [
            "",
            '[instances.demo]\nendpoint = "not-a-url"\n',
            '[instances.demo]\nendpoint = "http://user:secret@example/mcp"\n',
        ],
    )
    def test_rejects_invalid_instance_config(self, engram_gateway, tmp_path, contents):
        config = tmp_path / "instances.toml"
        config.write_text(contents)

        with pytest.raises(ValueError):
            engram_gateway.load_instance_registry(config)

    @pytest.mark.parametrize(
        ("contents", "message"),
        [
            (
                "[instances.demo.backends.docs]\nkind = \"smtp\"\nurl = \"http://example/mcp\"\n",
                "kind must be",
            ),
            (
                "[instances.demo.backends.docs]\nkind = \"http\"\nurl = \"http://user:secret@example/mcp\"\n",
                "must not contain credentials",
            ),
            (
                "[instances.demo.backends.code]\nkind = \"stdio\"\ncommand = \"x\"\nargs = \"not-an-array\"\n",
                "args must be",
            ),
            (
                "[instances.demo.backends.code]\nkind = \"http\"\nurl = \"http://example/mcp\"\ntimeout_seconds = 0\n",
                "timeout_seconds must be",
            ),
            (
                "[instances.demo.backends.code]\nkind = \"http\"\nurl = \"http://example/mcp\"\ntimeout_seconds = 601\n",
                "timeout_seconds must be",
            ),
        ],
    )
    def test_rejects_invalid_direct_backend_config(self, engram_gateway, tmp_path, contents, message):
        config = tmp_path / "instances.toml"
        config.write_text(contents)

        with pytest.raises(ValueError, match=message):
            engram_gateway.load_instance_registry(config)

    def test_rejects_mixing_legacy_endpoint_and_direct_backends(self, engram_gateway, tmp_path):
        config = tmp_path / "instances.toml"
        config.write_text(
            """
[instances.demo]
endpoint = "http://example/mcp/demo"

[instances.demo.backends.docs]
kind = "http"
url = "http://example/mcp/docs"
"""
        )

        with pytest.raises(ValueError, match="both endpoint and backends"):
            engram_gateway.load_instance_registry(config)

    def test_dynamic_registry_builds_one_route_per_instance(self, engram_gateway, tmp_path):
        config = tmp_path / "instances.toml"
        config.write_text('[instances.demo]\nendpoint = "http://host.containers.internal:9001/mcp/demo"\n')

        registry = engram_gateway.load_instance_registry(config)
        adapters = engram_gateway.build_backend_adapters(registry)
        app = engram_gateway.build_app(adapters)

        assert {route.path for route in app.routes} == {"/mcp/demo"}
        assert adapters["demo"]["host"].url == "http://host.containers.internal:9001/mcp/demo"
        assert adapters["demo"]["host"].timeout_seconds == engram_gateway.FORWARD_TIMEOUT_S


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
