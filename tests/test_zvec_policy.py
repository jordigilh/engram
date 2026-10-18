"""Focused coverage for the fail-closed zvec policy boundary."""
from __future__ import annotations

import json
import pathlib
import subprocess
from types import SimpleNamespace

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


def test_git_workspace_identity_fails_closed_for_unusable_git_output(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()

    def raise_os_error(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(zvec_policy.subprocess, "run", raise_os_error)
    assert zvec_policy.git_workspace_identity(root) is None

    monkeypatch.setattr(
        zvec_policy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert zvec_policy.git_workspace_identity(root) is None

    monkeypatch.setattr(
        zvec_policy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="only-one-line\n"),
    )
    assert zvec_policy.git_workspace_identity(root) is None

    relative_top = root / "relative-top"
    (relative_top / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        zvec_policy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="relative-top\n.git\n"),
    )
    assert zvec_policy.git_workspace_identity(root) == (relative_top.resolve(), (relative_top / ".git").resolve())

    regular_file = root / "not-a-directory"
    regular_file.write_text("file\n")
    common = root / "common"
    common.mkdir()
    monkeypatch.setattr(
        zvec_policy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{regular_file}\n{common}\n",
        ),
    )
    assert zvec_policy.git_workspace_identity(root) is None

    monkeypatch.setattr(
        zvec_policy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{root / 'missing'}\n{common}\n",
        ),
    )
    assert zvec_policy.git_workspace_identity(root) is None


def test_registered_git_worktrees_ignores_invalid_entries_and_git_failures(tmp_path, monkeypatch):
    valid = tmp_path / "valid"
    valid.mkdir()
    regular_file = tmp_path / "file"
    regular_file.write_text("file\n")
    missing = tmp_path / "missing"
    output = (
        f"worktree {valid}\n"
        "HEAD abc\n"
        "not-a-worktree-entry\n"
        f"worktree {missing}\n"
        f"worktree {regular_file}\n"
    )
    monkeypatch.setattr(
        zvec_policy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    assert zvec_policy._registered_git_worktrees(tmp_path) == frozenset({valid.resolve()})

    def raise_subprocess_error(*args, **kwargs):
        raise subprocess.SubprocessError("git failed")

    monkeypatch.setattr(zvec_policy.subprocess, "run", raise_subprocess_error)
    assert zvec_policy._registered_git_worktrees(tmp_path) == frozenset()

    monkeypatch.setattr(
        zvec_policy.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert zvec_policy._registered_git_worktrees(tmp_path) == frozenset()


def test_git_worktree_match_requires_both_git_identities(monkeypatch, tmp_path):
    monkeypatch.setattr(zvec_policy, "git_workspace_identity", lambda root: None)
    assert not zvec_policy.is_git_worktree_of(tmp_path / "requested", tmp_path / "allowed")


def test_authorized_root_accepts_only_a_registered_linked_worktree_of_an_allowed_checkout(tmp_path, monkeypatch):
    for variable in zvec_policy._GIT_REPOSITORY_ENVIRONMENT:
        monkeypatch.delenv(variable, raising=False)
    canonical = tmp_path / "canonical"
    linked = tmp_path / "worktree"
    unrelated = tmp_path / "unrelated"
    canonical.mkdir()
    subprocess.run(["git", "-C", str(canonical), "init", "--quiet"], check=True)
    subprocess.run(
        [
            "git", "-C", str(canonical), "-c", "user.name=Engram Test",
            "-c", "user.email=engram-test@example.com", "commit", "--quiet", "--allow-empty", "-m", "init",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(canonical), "worktree", "add", "--quiet", "--detach", str(linked)], check=True)

    allowed_roots = frozenset({canonical})
    assert zvec_policy.authorized_zvec_root(str(linked), allowed_roots) == linked.resolve()

    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / ".git").write_text(f"gitdir: {canonical / '.git'}\n")
    assert zvec_policy.authorized_zvec_root(str(forged), allowed_roots) is None

    nested = linked / "nested"
    nested.mkdir()
    assert zvec_policy.authorized_zvec_root(str(nested), allowed_roots) is None

    unrelated.mkdir()
    subprocess.run(["git", "-C", str(unrelated), "init", "--quiet"], check=True)
    assert zvec_policy.authorized_zvec_root(str(unrelated), allowed_roots) is None

    monkeypatch.setenv("GIT_DIR", str(canonical / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(unrelated))
    assert zvec_policy.authorized_zvec_root(str(linked), allowed_roots) == linked.resolve()
