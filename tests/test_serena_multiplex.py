"""Tests for serena_multiplex.py -- the HTTP wrapper in front of the single
shared kubernaut-family Serena daemon (see docs/findings/2026-08.md,
2026-08-13, ninth follow-up).

Business problem under test: Serena's `activate_project` state lives at the
*process* level on the shared daemon, not per MCP session (verified
empirically -- a second, independent session's activate_project call changes
what an already-connected first session sees). Left unguarded, two Cursor
windows on two different kubernaut-family repos race: whichever
activate_project call lands last wins for *both*, silently returning results
for the wrong repo. This module fixes that by giving each repo its own fixed
mount (e.g. /mcp/kubernaut-operator) that transparently re-asserts its own
project immediately before every tool call, serialized behind a lock so two
mounts' activate+call sequences can never interleave.

An earlier version of this module proxied via fastmcp's Client/create_proxy,
which keeps a persistent SSE listen stream open with the upstream and was
found (live, during the 2026-08-13 spike) to crash the shared Serena daemon
on reconnect -- a known, still-open upstream bug class (client disconnects
mid-SSE-stream -> ASGI response lifecycle violation). This module instead
hand-rolls a minimal one-shot, POST-only relay, so no real Serena daemon,
HTTP server, or SSE stream is exercised here -- only the pure
activation/serialization/routing/SSE-body-parsing logic, with fakes standing
in for the upstream HTTP calls.
"""
from __future__ import annotations

import asyncio
import json


class TestActiveProjectTracker:
    def test_first_pin_activates_the_project(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        activated = []

        async def activate(project):
            activated.append(project)

        async def run():
            async with tracker.pinned("kubernaut-operator", activate):
                pass

        asyncio.run(run())

        assert activated == ["kubernaut-operator"]
        assert tracker.active == "kubernaut-operator"

    def test_repeated_pin_of_same_project_skips_activate(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        activated = []

        async def activate(project):
            activated.append(project)

        async def run():
            async with tracker.pinned("kubernaut", activate):
                pass
            async with tracker.pinned("kubernaut", activate):
                pass

        asyncio.run(run())

        assert activated == ["kubernaut"]  # only the first pin actually switched

    def test_switching_project_calls_activate_again(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        activated = []

        async def activate(project):
            activated.append(project)

        async def run():
            async with tracker.pinned("kubernaut-operator", activate):
                pass
            async with tracker.pinned("kubernaut-console", activate):
                pass

        asyncio.run(run())

        assert activated == ["kubernaut-operator", "kubernaut-console"]
        assert tracker.active == "kubernaut-console"

    def test_activate_failure_propagates_and_leaves_active_project_unchanged(
        self, serena_multiplex
    ):
        tracker = serena_multiplex.ActiveProjectTracker()

        async def activate(project):
            raise RuntimeError("upstream unreachable")

        async def run():
            async with tracker.pinned("kubernaut-operator", activate):
                pass

        with __import__("pytest").raises(RuntimeError):
            asyncio.run(run())

        assert tracker.active is None  # failed switch must not be recorded as active

    def test_concurrent_pins_for_different_projects_are_serialized(self, serena_multiplex):
        """Two mounts racing for different projects must not interleave --
        the whole activate+call-body of one pin must complete before the
        other's activate begins."""
        tracker = serena_multiplex.ActiveProjectTracker()
        events: list[str] = []

        async def activate_a(project):
            events.append(f"activate-start:{project}")
            await asyncio.sleep(0.05)  # long enough for B to attempt to jump in
            events.append(f"activate-end:{project}")

        async def activate_b(project):
            events.append(f"activate-start:{project}")
            events.append(f"activate-end:{project}")

        async def task_a():
            async with tracker.pinned("kubernaut-operator", activate_a):
                events.append("body:kubernaut-operator")
                await asyncio.sleep(0.02)

        async def task_b():
            await asyncio.sleep(0.01)  # start while A is still mid-activate
            async with tracker.pinned("kubernaut-console", activate_b):
                events.append("body:kubernaut-console")

        async def run():
            await asyncio.gather(task_a(), task_b())

        asyncio.run(run())

        # B's activate must not start until A's entire pinned block (activate + body) finished.
        a_body_index = events.index("body:kubernaut-operator")
        b_activate_index = events.index("activate-start:kubernaut-console")
        assert b_activate_index > a_body_index


class TestActiveProjectTrackerInvalidate:
    def test_invalidate_clears_active_project(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        tracker.active = "kubernaut-operator"

        asyncio.run(tracker.invalidate())

        assert tracker.active is None

    def test_pinned_reactivates_after_invalidate_for_the_same_project(self, serena_multiplex):
        """Regression guard for the 2026-08-20 restart-storm bug: a cache hit
        (same project as last time) must not be trusted forever -- once
        invalidated, the next pin for the *same* project must re-activate,
        not skip activation just because the project name didn't change."""
        tracker = serena_multiplex.ActiveProjectTracker()
        activated = []

        async def activate(project):
            activated.append(project)

        async def run():
            async with tracker.pinned("kubernaut-operator", activate):
                pass
            await tracker.invalidate()
            async with tracker.pinned("kubernaut-operator", activate):
                pass

        asyncio.run(run())

        assert activated == ["kubernaut-operator", "kubernaut-operator"]


class TestLooksLikeNoActiveProjectError:
    def test_detects_no_active_project_tool_error(self, serena_multiplex):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "No active project. Ask the user to provide the project path...",
                        }
                    ],
                    "isError": True,
                },
            }
        ).encode()

        assert serena_multiplex._looks_like_no_active_project_error(body) is True

    def test_ignores_successful_result(self, serena_multiplex):
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": False}}
        ).encode()

        assert serena_multiplex._looks_like_no_active_project_error(body) is False

    def test_ignores_unrelated_tool_error(self, serena_multiplex):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "file not found: foo.go"}],
                    "isError": True,
                },
            }
        ).encode()

        assert serena_multiplex._looks_like_no_active_project_error(body) is False

    def test_handles_non_json_body_gracefully(self, serena_multiplex):
        assert serena_multiplex._looks_like_no_active_project_error(b"not json at all") is False

    def test_handles_sse_framed_body(self, serena_multiplex):
        body = (
            b'event: message\ndata: {"jsonrpc": "2.0", "id": 1, "result": '
            b'{"content": [{"type": "text", "text": "No active project."}], "isError": true}}\n\n'
        )

        assert serena_multiplex._looks_like_no_active_project_error(body) is True


class TestForwardScoped:
    """Covers the retry wrapper used by the real HTTP endpoint (see
    build_app) instead of the bare `async with tracker.pinned(...)` -- see
    docs/findings/2026-08.md, 2026-08-20 entry: the shared upstream Serena
    daemon restarts roughly every 10 minutes (a deliberate side effect of the
    watch-mirror sync's reference-transaction hook), which silently wipes the
    upstream's real active-project state without this multiplex's own
    ActiveProjectTracker cache finding out. Left unguarded, every mount
    that isn't the most recently *switched-to* project starts getting
    "No active project" errors on every call until something else forces a
    cache invalidation."""

    class _FakeResponse:
        def __init__(self, body: bytes):
            self.body = body

    _OK_BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"isError": False}}).encode()
    _STALE_BODY = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "No active project. Ask the user..."}],
                "isError": True,
            },
        }
    ).encode()

    def test_success_on_first_try_does_not_retry(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        activated: list[str] = []
        calls = 0

        async def activate(project):
            activated.append(project)

        async def forward_raw():
            nonlocal calls
            calls += 1
            return self._FakeResponse(self._OK_BODY)

        result = asyncio.run(
            serena_multiplex._forward_scoped("kubernaut-operator", tracker, activate, forward_raw)
        )

        assert activated == ["kubernaut-operator"]
        assert calls == 1
        assert result.body == self._OK_BODY
        assert tracker.active == "kubernaut-operator"

    def test_stale_cache_after_upstream_restart_triggers_reactivate_and_retry(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        tracker.active = "kubernaut-operator"  # cache thinks it's already pinned
        activated: list[str] = []
        calls = 0

        async def activate(project):
            activated.append(project)

        async def forward_raw():
            nonlocal calls
            calls += 1
            return self._FakeResponse(self._STALE_BODY if calls == 1 else self._OK_BODY)

        result = asyncio.run(
            serena_multiplex._forward_scoped("kubernaut-operator", tracker, activate, forward_raw)
        )

        # First pin was a cache hit (no activate) -- only the retry, after
        # the stale response forced an invalidation, actually re-activates.
        assert activated == ["kubernaut-operator"]
        assert calls == 2
        assert result.body == self._OK_BODY
        assert tracker.active == "kubernaut-operator"

    def test_persistent_upstream_failure_retries_only_once(self, serena_multiplex):
        """Must not loop forever if the upstream is genuinely down/broken --
        one retry, then return whatever it gets."""
        tracker = serena_multiplex.ActiveProjectTracker()
        calls = 0

        async def activate(project):
            pass

        async def forward_raw():
            nonlocal calls
            calls += 1
            return self._FakeResponse(self._STALE_BODY)

        result = asyncio.run(
            serena_multiplex._forward_scoped("kubernaut-operator", tracker, activate, forward_raw)
        )

        assert calls == 2
        assert result.body == self._STALE_BODY

    def test_transient_upstream_disconnect_reactivates_and_retries(self, serena_multiplex, monkeypatch):
        tracker = serena_multiplex.ActiveProjectTracker()
        activated: list[str] = []
        calls = 0

        async def activate(project):
            activated.append(project)

        async def forward_raw():
            nonlocal calls
            calls += 1
            if calls == 1:
                import httpx

                raise httpx.ConnectError("upstream restarting")
            return self._FakeResponse(self._OK_BODY)

        monkeypatch.setattr(serena_multiplex, "RETRY_DELAY_S", 0)
        result = asyncio.run(
            serena_multiplex._forward_scoped("kubernaut", tracker, activate, forward_raw)
        )

        assert activated == ["kubernaut", "kubernaut"]
        assert calls == 2
        assert result.body == self._OK_BODY

    def test_non_transient_forward_error_is_not_swallowed(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()

        async def activate(project):
            pass

        async def forward_raw():
            raise ValueError("bad response handling")

        with __import__("pytest").raises(ValueError):
            asyncio.run(
                serena_multiplex._forward_scoped("kubernaut", tracker, activate, forward_raw)
            )


class TestIsProjectAgnosticTool:
    def test_query_project_and_list_queryable_projects_are_agnostic(self, serena_multiplex):
        assert serena_multiplex.is_project_agnostic_tool("query_project") is True
        assert serena_multiplex.is_project_agnostic_tool("list_queryable_projects") is True

    def test_scoped_tools_are_not_agnostic(self, serena_multiplex):
        for name in ("find_symbol", "find_referencing_symbols", "get_current_config", "get_symbols_overview"):
            assert serena_multiplex.is_project_agnostic_tool(name) is False

    def test_unknown_future_tool_defaults_to_scoped(self, serena_multiplex):
        """Fail-safe: a Serena tool this module doesn't know about yet must
        default to being pinned/activated-before-call, not silently
        unprotected."""
        assert serena_multiplex.is_project_agnostic_tool("some_brand_new_tool") is False


class TestRouteToolCall:
    def test_activate_project_is_intercepted(self, serena_multiplex):
        assert serena_multiplex.route_tool_call("activate_project") == "intercept"

    def test_query_project_is_agnostic(self, serena_multiplex):
        assert serena_multiplex.route_tool_call("query_project") == "agnostic"

    def test_find_symbol_is_scoped(self, serena_multiplex):
        assert serena_multiplex.route_tool_call("find_symbol") == "scoped"

    def test_unknown_tool_defaults_to_scoped(self, serena_multiplex):
        assert serena_multiplex.route_tool_call("some_brand_new_tool") == "scoped"


class TestHandleJsonRpcMessage:
    def test_non_tool_call_methods_pass_through_untouched(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        record: list[str] = []

        async def activate(project):
            record.append(f"activate:{project}")

        async def forward(message):
            record.append(f"forward:{message['method']}")
            return "forwarded"

        message = {"jsonrpc": "2.0", "method": "notifications/initialized"}

        result = asyncio.run(
            serena_multiplex.handle_json_rpc_message(
                message, "kubernaut-operator", tracker, activate, forward
            )
        )

        assert record == ["forward:notifications/initialized"]
        assert result == "forwarded"

    def test_activate_project_call_is_intercepted_and_not_forwarded(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        record: list[str] = []

        async def activate(project):
            record.append(f"activate:{project}")

        async def forward(message):
            record.append("forward")
            return "forwarded"

        message = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "activate_project", "arguments": {"project": "kubernaut-console"}},
        }

        result = asyncio.run(
            serena_multiplex.handle_json_rpc_message(
                message, "kubernaut-operator", tracker, activate, forward
            )
        )

        assert record == []  # neither the real activate nor forward ran
        assert result["id"] == 7  # echoes the caller's own JSON-RPC id
        assert "kubernaut-operator" in result["result"]["content"][0]["text"]

    def test_scoped_tool_call_activates_before_forwarding(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        record: list[str] = []

        async def activate(project):
            record.append(f"activate:{project}")

        async def forward(message):
            record.append(f"forward:{message['params']['name']}")
            return "forwarded"

        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "find_symbol"}}

        result = asyncio.run(
            serena_multiplex.handle_json_rpc_message(
                message, "kubernaut-operator", tracker, activate, forward
            )
        )

        assert record == ["activate:kubernaut-operator", "forward:find_symbol"]
        assert result == "forwarded"

    def test_scoped_tool_call_when_already_pinned_skips_activate_but_still_forwards(
        self, serena_multiplex
    ):
        tracker = serena_multiplex.ActiveProjectTracker()
        record: list[str] = []

        async def activate(project):
            record.append(f"activate:{project}")

        async def forward(message):
            record.append(f"forward:{message['params']['name']}")
            return "forwarded"

        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "find_symbol"}}

        async def run():
            await serena_multiplex.handle_json_rpc_message(
                message, "kubernaut-operator", tracker, activate, forward
            )
            return await serena_multiplex.handle_json_rpc_message(
                message, "kubernaut-operator", tracker, activate, forward
            )

        asyncio.run(run())

        assert record == [
            "activate:kubernaut-operator",
            "forward:find_symbol",
            "forward:find_symbol",
        ]  # second call skipped re-activation

    def test_agnostic_tool_passthrough_without_activation(self, serena_multiplex):
        tracker = serena_multiplex.ActiveProjectTracker()
        record: list[str] = []

        async def activate(project):
            record.append(f"activate:{project}")

        async def forward(message):
            record.append(f"forward:{message['params']['name']}")
            return "forwarded"

        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "query_project", "arguments": {"project": "kubernaut-console"}},
        }

        result = asyncio.run(
            serena_multiplex.handle_json_rpc_message(
                message, "kubernaut-operator", tracker, activate, forward
            )
        )

        assert record == ["forward:query_project"]  # no activate for an explicitly-targeted, agnostic tool
        assert result == "forwarded"


class TestParseSseJson:
    def test_parses_sse_framed_body(self, serena_multiplex):
        body = b'event: message\ndata: {"jsonrpc": "2.0", "id": 1, "result": {}}\n\n'

        result = serena_multiplex._parse_sse_json(body)

        assert result == {"jsonrpc": "2.0", "id": 1, "result": {}}

    def test_parses_plain_json_body(self, serena_multiplex):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()

        result = serena_multiplex._parse_sse_json(body)

        assert result == {"jsonrpc": "2.0", "id": 1, "result": {}}


class TestBuildApp:
    def test_mounts_one_route_per_requested_project(self, serena_multiplex):
        app = serena_multiplex.build_app(
            projects=["kubernaut", "kubernaut-operator"],
            upstream_url="http://127.0.0.1:8892/mcp",
        )

        paths = {route.path for route in app.routes}
        assert paths == {"/mcp/kubernaut", "/mcp/kubernaut-operator"}

    def test_only_requested_projects_are_mounted(self, serena_multiplex):
        app = serena_multiplex.build_app(
            projects=["kubernaut-console"],
            upstream_url="http://127.0.0.1:8892/mcp",
        )

        paths = {route.path for route in app.routes}
        assert paths == {"/mcp/kubernaut-console"}

    def test_get_requests_are_declined_with_405(self, serena_multiplex):
        """Regression guard for the design decision in the module docstring:
        this proxy never opens/offers an SSE listen stream, so GET must be
        declined rather than hung or silently accepted."""
        from starlette.testclient import TestClient

        app = serena_multiplex.build_app(
            projects=["kubernaut"], upstream_url="http://127.0.0.1:8892/mcp"
        )
        client = TestClient(app)

        response = client.get("/mcp/kubernaut")

        assert response.status_code == 405
