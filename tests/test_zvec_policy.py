"""Focused coverage for the fail-closed zvec policy boundary."""
from __future__ import annotations

import json
import pathlib

import pytest

from engram.pipeline import zvec_policy


def test_managed_arguments_cover_search_graph_and_unrelated_tools():
    arguments = {"root": "/workspace", "query": "fresh"}
    assert zvec_policy.managed_zvec_arguments("zvec_grep_search", arguments)["freshness"] == "wait_for_fresh"
    assert zvec_policy.managed_zvec_arguments("zvec_grep_codegraph_node", arguments)["autoUpdate"] is True
    assert zvec_policy.managed_zvec_arguments("recall", arguments) is arguments


def test_redaction_handles_scalars_sequences_nested_values_and_primitives():
    assert zvec_policy.redact_shadow_value("secret", "query") == {"redacted": True, "chars": 6}
    assert zvec_policy.redact_shadow_value(["a", "b"], "content") == {"redacted": True, "items": 2}
    assert zvec_policy.redact_shadow_value(("a",), "response") == {"redacted": True, "items": 1}
    assert zvec_policy.redact_shadow_value(("a",)) == ["a"]
    assert zvec_policy.redact_shadow_value(42, "query") == {"redacted": True}
    assert zvec_policy.redact_shadow_value({"text": "secret", "count": 2}) == {
        "text": {"redacted": True, "chars": 6},
        "count": 2,
    }
    assert zvec_policy.redact_shadow_value([{"query": "secret"}]) == [{"query": {"redacted": True, "chars": 6}}]
    assert zvec_policy.redact_shadow_value(42) == 42


def test_append_shadow_jsonl_redacts_content_and_applies_private_modes(tmp_path):
    path = tmp_path / "logs" / "shadow.jsonl"
    zvec_policy.append_shadow_jsonl(
        path,
        {"arguments": {"query": "secret"}, "primary": {"response": {"text": "source"}}},
    )

    entry = json.loads(path.read_text())
    assert entry["redaction"] == "query-and-response-content-v1"
    assert entry["arguments"]["query"]["redacted"] is True
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_append_shadow_jsonl_survives_chmod_failure(tmp_path, monkeypatch):
    path = tmp_path / "shadow.jsonl"

    def fail_chmod(self, mode):
        raise OSError("read-only metadata")

    monkeypatch.setattr(pathlib.Path, "chmod", fail_chmod)
    zvec_policy.append_shadow_jsonl(path, {"text": "safe"})
    assert path.exists()


def test_append_shadow_jsonl_closes_fd_when_stream_open_fails(tmp_path, monkeypatch):
    path = tmp_path / "shadow.jsonl"

    def fail_fdopen(*args, **kwargs):
        raise OSError("cannot open stream")

    monkeypatch.setattr(zvec_policy.os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="cannot open stream"):
        zvec_policy.append_shadow_jsonl(path, {})


def test_authorized_root_requires_existing_exact_real_path(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    allowed_roots = frozenset({allowed})

    assert zvec_policy.authorized_zvec_root(str(allowed), allowed_roots) == allowed.resolve()
    assert zvec_policy.authorized_zvec_root(str(tmp_path / "other"), allowed_roots) is None
    assert zvec_policy.authorized_zvec_root(None, allowed_roots) is None
    assert zvec_policy.authorized_zvec_root(str(allowed), frozenset()) is None

    link = tmp_path / "link"
    link.symlink_to(allowed, target_is_directory=True)
    assert zvec_policy.authorized_zvec_root(str(link), allowed_roots) == allowed.resolve()
    assert zvec_policy.authorized_zvec_root(str(allowed), frozenset({tmp_path / "missing"})) is None
