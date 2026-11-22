"""Tests for per-workspace invalidation (issue #204).

Covers the state machine in ``serena_workspaces`` and the multiplex admin
endpoint/typed response wiring. The upstream Serena process is never started;
control calls are fakes.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from engram.pipeline import serena_multiplex, serena_workspaces


def _probe(head="a", lock=False, error=None):
    return serena_workspaces.ProbeResult(
        head=head, fingerprint=f"{head}:{lock}:{error}", lock_held=lock, error=error
    )


class TestWorkspaceRuntime:
    def _runtime(self, probes, *, activate=None, restart=None):
        calls = {"activate": 0, "restart": 0}

        async def _activate(_workspace):
            calls["activate"] += 1
            if activate:
                await activate()

        async def _restart(_workspace):
            calls["restart"] += 1
            if restart:
                await restart()

        runtime = serena_workspaces.WorkspaceRuntime(
            "/tmp/ws",
            _activate,
            _restart,
            probe=probes,
            settle_s=0,
            queue_timeout_s=0.1,
        )
        return runtime, calls

    def test_invalidate_bumps_only_this_workspace_and_dedups(self):
        runtime, _ = self._runtime(lambda _ws: _probe())
        asyncio.run(runtime.invalidate("m1"))
        epoch = runtime.state.epoch
        assert asyncio.run(runtime.invalidate("m1")) is False
        assert runtime.state.epoch == epoch

        other, _ = self._runtime(lambda _ws: _probe())
        asyncio.run(other.invalidate("m1"))
        assert other.state.epoch == 1
        assert runtime.state.epoch == epoch

    def test_read_after_invalidation_restarts_then_serves(self):
        heads = {"head": "a"}

        def probe(_ws):
            return _probe(heads["head"])

        async def forward():
            return "ok"

        runtime, calls = self._runtime(probe)
        assert asyncio.run(runtime.serve("find_symbol", forward)) == "ok"
        assert calls["restart"] == 1  # first call is fail-closed -> restart
        asyncio.run(runtime.invalidate("m1"))
        assert asyncio.run(runtime.serve("find_symbol", forward)) == "ok"
        assert calls["restart"] == 2

    def test_read_returns_typed_flushing_when_epoch_changes_mid_call(self):
        runtime, _ = self._runtime(lambda _ws: _probe())

        async def forward():
            await runtime.invalidate("m-during")
            return "stale"

        with pytest.raises(serena_workspaces.WorkspaceBusy) as exc:
            asyncio.run(runtime.serve("find_symbol", forward))
        assert exc.value.kind == "workspace_flushing"

    def test_write_returns_outcome_unknown_when_epoch_changes_mid_call(self):
        runtime, _ = self._runtime(lambda _ws: _probe())

        async def forward():
            await runtime.invalidate("m-during")
            return "maybe-applied"

        with pytest.raises(serena_workspaces.WorkspaceBusy) as exc:
            asyncio.run(runtime.serve("rename_symbol", forward))
        assert exc.value.kind == "outcome_unknown"
        assert exc.value.retryable is False

    def test_activation_failure_stays_flushing(self):
        async def boom():
            raise RuntimeError("restart unavailable")

        runtime, _ = self._runtime(lambda _ws: _probe(), restart=boom)

        async def forward():  # pragma: no cover - must not run
            raise AssertionError("forward must not run")

        with pytest.raises(serena_workspaces.WorkspaceBusy) as exc:
            asyncio.run(runtime.serve("find_symbol", forward))
        assert exc.value.kind == "workspace_flushing"
        assert runtime.state.state == "flushing"

    def test_lock_held_probe_never_forwards(self):
        runtime, _ = self._runtime(lambda _ws: _probe(lock=True))

        async def forward():  # pragma: no cover - must not run
            raise AssertionError("forward must not run")

        with pytest.raises(serena_workspaces.WorkspaceBusy):
            asyncio.run(runtime.serve("find_symbol", forward))

    def test_queue_wait_is_bounded(self):
        runtime, _ = self._runtime(lambda _ws: _probe())

        async def scenario():
            async with runtime._gate:
                return await runtime.serve("find_symbol", lambda: None)

        with pytest.raises(serena_workspaces.WorkspaceBusy) as exc:
            asyncio.run(scenario())
        assert exc.value.kind == "workspace_flushing"

    def test_write_holds_shared_barrier_during_forward(self, tmp_path):
        import fcntl
        import os

        lock = tmp_path / "barrier.lock"
        runtime, _ = self._runtime(lambda _ws: _probe())
        runtime.barrier = serena_workspaces.WorkspaceBarrier(str(lock))

        async def forward():
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            return "ok"

        assert asyncio.run(runtime.serve("rename_symbol", forward)) == "ok"

    def test_write_rejects_head_drift_before_dispatch(self, tmp_path):
        probes = [_probe("a"), _probe("a"), _probe("a"), _probe("b")]
        runtime, _ = self._runtime(lambda _ws: probes.pop(0))
        runtime.barrier = serena_workspaces.WorkspaceBarrier(
            str(tmp_path / "barrier.lock"), runtime.workspace
        )

        async def forward():  # pragma: no cover - must not run
            raise AssertionError("forward must not run")

        with pytest.raises(serena_workspaces.WorkspaceBusy) as exc:
            asyncio.run(runtime.serve("rename_symbol", forward))
        assert exc.value.kind == "workspace_flushing"

    def test_write_fails_typed_when_git_holds_barrier(self, tmp_path):
        import fcntl
        import os

        lock = tmp_path / "barrier.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            runtime, _ = self._runtime(lambda _ws: _probe())
            runtime.barrier = serena_workspaces.WorkspaceBarrier(
                str(lock), runtime.workspace
            )
            runtime.barrier_timeout_s = 0.1

            async def forward():  # pragma: no cover - must not run
                raise AssertionError("forward must not run")

            with pytest.raises(serena_workspaces.WorkspaceBusy) as exc:
                asyncio.run(runtime.serve("rename_symbol", forward))
            assert exc.value.kind == "workspace_flushing"
            assert exc.value.workspace == runtime.workspace
        finally:
            os.close(fd)


class TestTypedPayloads:
    def test_flushing_payload_shape(self):
        busy = serena_workspaces.WorkspaceBusy(
            "workspace_flushing", "/ws", epoch=3, retry_after_ms=1500
        )
        payload = serena_workspaces.typed_error_payload(7, busy.kind, busy.workspace,
                                                        epoch=busy.epoch,
                                                        retry_after_ms=busy.retry_after_ms)
        assert payload["error"]["code"] == -32002
        assert payload["error"]["data"]["kind"] == "workspace_flushing"
        assert payload["error"]["data"]["epoch"] == 3
        assert serena_workspaces.header_for_busy(busy)["X-Engram-Error"] == "workspace_flushing"

    def test_read_only_allowlist_and_write_default(self):
        assert serena_workspaces.is_read_only_tool("find_symbol") is True
        assert serena_workspaces.is_read_only_tool("rename_symbol") is False
        assert serena_workspaces.is_read_only_tool("brand_new_tool") is False

    def test_internal_control_tools_are_intercepted(self):
        assert serena_multiplex.route_tool_call("activate_project") == "intercept"
        assert serena_multiplex.route_tool_call("restart_language_server") == "intercept"


class TestAdminEndpoint:
    def _app(self, tmp_path, token="secret"):
        return serena_multiplex.build_app(
            ["proj"],
            "http://127.0.0.1:9/mcp",
            allowed_roots=[str(tmp_path)],
            invalidation_token=token,
            require_workspace="enforce",
            allow_shared_invalidation=True,
            lsp_readiness_symbol="Ready",
        )

    def test_invalidate_bumps_epoch_and_requires_token(self, tmp_path):
        client = TestClient(self._app(tmp_path))
        body = {
            "version": 1,
            "workspace": str(tmp_path),
            "mutation_id": "m1",
            "transaction_id": "t1",
            "phase": "begin",
            "reason": "git",
        }
        assert client.post("/admin/invalidate", json=body).status_code == 403
        response = client.post(
            "/admin/invalidate",
            json=body,
            headers={"X-Engram-Invalidation-Token": "secret"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["epoch"] == 1
        assert data["state"] == "flushing"
        assert data["applied"] is True

        duplicate = client.post(
            "/admin/invalidate",
            json=body,
            headers={"X-Engram-Invalidation-Token": "secret"},
        ).json()
        assert duplicate["applied"] is False
        assert duplicate["reason"] == "duplicate"
        assert duplicate["epoch"] == 1

    def test_recover_clears_transaction_and_tombstones_it(self, tmp_path):
        client = TestClient(self._app(tmp_path))
        headers = {"X-Engram-Invalidation-Token": "secret"}
        begin = {
            "version": 1,
            "workspace": str(tmp_path),
            "mutation_id": "m1",
            "transaction_id": "t1",
            "phase": "begin",
        }
        assert client.post("/admin/invalidate", json=begin, headers=headers).status_code == 200
        recovered = client.post(
            "/admin/recover",
            json={
                "workspace": str(tmp_path),
                "transaction_id": "t1",
                "reason": "operator",
                "force": True,
            },
            headers=headers,
        )
        assert recovered.status_code == 200
        assert recovered.json()["cleared"] == ["t1"]

        replay = client.post("/admin/invalidate", json=begin, headers=headers).json()
        assert replay["applied"] is False
        assert replay["reason"] == "tombstoned"

    def test_terminal_events_apply_once(self):
        async def noop(_workspace):
            return None

        runtime = serena_workspaces.WorkspaceRuntime(
            "/tmp/ws", noop, noop, probe=lambda _ws: _probe()
        )
        event = {
            "version": 1,
            "workspace": "/tmp/ws",
            "mutation_id": "m1",
            "transaction_id": "t1",
            "phase": "ref_committed",
        }
        first = asyncio.run(runtime.apply_event(event))
        epoch = runtime.state.epoch
        second = asyncio.run(runtime.apply_event(event))
        assert first["applied"] is True
        assert second["applied"] is False
        assert second["reason"] == "tombstoned"
        assert runtime.state.epoch == epoch

    def test_tombstones_survive_restart(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        event = {
            "version": 1,
            "workspace": "/tmp/ws",
            "mutation_id": "m1",
            "transaction_id": "t1",
            "phase": "begin",
        }

        async def build():
            async def noop(_workspace):
                return None

            return serena_workspaces.WorkspaceRuntime(
                "/tmp/ws",
                noop,
                noop,
                probe=lambda _ws: _probe(),
                state_path=state_path,
            )

        first = asyncio.run(build())
        asyncio.run(first.apply_event(event))
        asyncio.run(
            first.apply_event(
                {
                    "version": 1,
                    "workspace": "/tmp/ws",
                    "mutation_id": "m2",
                    "transaction_id": "t1",
                    "phase": "worktree_completed",
                }
            )
        )

        second = asyncio.run(build())
        replay = asyncio.run(second.apply_event(event))
        assert replay["applied"] is False
        assert replay["reason"] == "tombstoned"

    def test_recover_requires_reason(self, tmp_path):
        client = TestClient(self._app(tmp_path))
        response = client.post(
            "/admin/recover",
            json={"workspace": str(tmp_path), "transaction_id": "t1"},
            headers={"X-Engram-Invalidation-Token": "secret"},
        )
        assert response.status_code == 400

    def test_unauthorized_workspace_is_rejected(self, tmp_path):
        client = TestClient(self._app(tmp_path))
        body = {
            "workspace": str(tmp_path / "outside"),
            "mutation_id": "m1",
            "transaction_id": "t1",
            "phase": "begin",
        }
        response = client.post(
            "/admin/invalidate",
            json=body,
            headers={"X-Engram-Invalidation-Token": "secret"},
        )
        assert response.status_code == 403

    def test_enforce_rejects_unbound_initialize(self, tmp_path):
        client = TestClient(self._app(tmp_path))
        response = client.post(
            "/mcp/proj",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert response.status_code == 409
        assert response.headers["X-Engram-Error"] == "workspace_unbound"

    def test_duplicate_workspace_endpoints_are_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="distinct"):
            serena_multiplex.build_app(
                ["proj"],
                "http://127.0.0.1:9/mcp",
                allowed_roots=[str(tmp_path)],
                workspace_upstreams={
                    str(tmp_path): "http://127.0.0.1:9/mcp",
                    str(tmp_path / "other"): "http://127.0.0.1:9/mcp",
                },
            )

    def test_invalidation_requires_revision_readiness_probe(self, tmp_path):
        with pytest.raises(ValueError, match="readiness probe"):
            serena_multiplex.build_app(
                ["proj"],
                "http://127.0.0.1:9/mcp",
                allowed_roots=[str(tmp_path)],
                invalidation_token="secret",
                allow_shared_invalidation=True,
            )


class TestSerenaSessionCompatibility:
    def test_409_session_conflict_is_treated_as_session_loss(self):
        assert (
            serena_multiplex._looks_like_upstream_session_loss(
                409, b'{"error":"session conflict"}'
            )
            is True
        )
        assert (
            serena_multiplex._looks_like_upstream_session_loss(
                409, b'{"error":"project mismatch"}'
            )
            is False
        )

    def test_parses_internal_session_id_from_initial_instructions(self):
        body = (
            b'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":'
            b'{"content":[{"type":"text","text":"Your Serena session id is `abc123def`."}]}}\n\n'
        )
        assert (
            serena_multiplex._parse_serena_session_id(body) == "abc123def"
        )

    def test_activate_arguments_include_session_only_when_known(self):
        assert serena_multiplex._activate_arguments("/ws", None) == {"project": "/ws"}
        assert serena_multiplex._activate_arguments("/ws", "s1") == {
            "project": "/ws",
            "session": "s1",
        }


class TestSpoolDrain:
    def test_drains_and_removes_events(self, tmp_path):
        event = {
            "version": 1,
            "workspace": str(tmp_path),
            "mutation_id": "m1",
            "transaction_id": "t1",
            "phase": "begin",
        }
        (tmp_path / "e1.json").write_text(json.dumps(event))
        (tmp_path / "corrupt.json").write_text("{not json")
        events = serena_workspaces.drain_spool_dir(str(tmp_path))
        assert len(events) == 1
        assert not (tmp_path / "e1.json").exists()
        assert not (tmp_path / "corrupt.json").exists()

    def test_state_files_are_not_drained(self, tmp_path):
        state_dir = tmp_path / ".state"
        state_dir.mkdir()
        state = state_dir / "abc.state.json"
        state.write_text(json.dumps({"workspace": str(tmp_path), "phase": "x"}))
        non_event = tmp_path / "not-an-event.json"
        non_event.write_text(json.dumps({"workspace": str(tmp_path)}))
        assert serena_workspaces.read_spool_events(str(tmp_path)) == []
        assert state.exists()
        assert non_event.exists()


class TestProbeStability:
    def test_git_status_does_not_change_fingerprint(self, tmp_path):
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        (repo / "a.txt").write_text("a")
        subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=t@example.com",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "init",
            ],
            check=True,
        )
        before = serena_workspaces.probe_worktree(str(repo))
        subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], check=True)
        after = serena_workspaces.probe_worktree(str(repo))
        assert before.fingerprint == after.fingerprint


class TestAuthorizationTimeout:
    def test_git_timeout_is_not_a_denial(self, tmp_path, monkeypatch):
        import subprocess

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=2)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(serena_workspaces.WorkspaceAuthTimeout):
            serena_multiplex._authorize_workspace(
                str(worktree), frozenset({allowed.resolve()})
            )
