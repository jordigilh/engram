"""Pure, fail-closed policy helpers for managed zvec workspace reads.

This module is intentionally independent from MCP transport code. The policy
surface is small enough to test thoroughly: every managed tool gets the same
freshness barrier, workspace roots are exact real paths, and comparison logs
never persist source or query content.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
from typing import Any


ZVEC_WORKSPACE_READ_TOOLS = frozenset(
    {
        "zvec_grep_search",
        "zvec_search_code",
        "zvec_grep_callgraph_blast_radius",
        "zvec_grep_callgraph_shortest_path",
        "zvec_grep_callgraph_cluster",
        "zvec_grep_callgraph_communities",
        "zvec_grep_codegraph_affected",
        "zvec_grep_codegraph_node",
        "zvec_grep_codegraph_neighbors",
        "zvec_grep_codegraph_relation_path",
        "zvec_grep_codegraph_explain",
    }
)

_SHADOW_REDACTED_KEYS = frozenset(
    {"content", "data", "query", "queries", "response", "source", "snippet", "text", "vector"}
)


def managed_zvec_arguments(tool: str, arguments: dict) -> dict:
    """Require every managed workspace read to wait for a fresh index."""
    if tool not in ZVEC_WORKSPACE_READ_TOOLS:
        return arguments
    return {
        **arguments,
        "autoUpdate": True,
        "freshness": "wait_for_fresh",
    }


def redact_shadow_value(value: Any, key: str | None = None) -> Any:
    """Keep discrepancy diagnostics useful without persisting source content."""
    if key in _SHADOW_REDACTED_KEYS:
        if isinstance(value, str):
            return {"redacted": True, "chars": len(value)}
        if isinstance(value, (list, tuple, dict)):
            return {"redacted": True, "items": len(value)}
        return {"redacted": True}
    if isinstance(value, dict):
        return {str(item_key): redact_shadow_value(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [redact_shadow_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_shadow_value(item) for item in value]
    return value


def append_shadow_jsonl(path: pathlib.Path, entry: dict[str, Any]) -> None:
    """Append a redacted comparison record with private directory/file modes."""
    path = path.expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    safe_entry = redact_shadow_value(entry)
    safe_entry["redaction"] = "query-and-response-content-v1"
    file_descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        with os.fdopen(file_descriptor, "a", encoding="utf-8") as stream:
            file_descriptor = -1
            stream.write(json.dumps(safe_entry, default=str, separators=(",", ":")) + "\n")
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def git_workspace_identity(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path] | None:
    """Return a Git worktree's top-level and shared Git directory.

    ``git-common-dir`` is shared by a main checkout and all of its linked
    worktrees. Requiring the requested path to be Git's top-level directory
    keeps a request for an arbitrary subdirectory from inheriting the
    checkout's authorization.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    try:
        top_level = pathlib.Path(lines[0]).expanduser()
        if not top_level.is_absolute():
            top_level = root / top_level
        top_level = top_level.resolve(strict=True)
        common_dir = pathlib.Path(lines[1]).expanduser()
        if not common_dir.is_absolute():
            common_dir = top_level / common_dir
        common_dir = common_dir.resolve(strict=True)
    except OSError:
        return None
    if not top_level.is_dir() or not common_dir.is_dir():
        return None
    return top_level, common_dir


def is_git_worktree_of(root: pathlib.Path, allowed_root: pathlib.Path) -> bool:
    """Return whether ``root`` is a top-level worktree of ``allowed_root``."""
    requested_identity = git_workspace_identity(root)
    allowed_identity = git_workspace_identity(allowed_root)
    if requested_identity is None or allowed_identity is None:
        return False
    requested_top, requested_common = requested_identity
    allowed_top, allowed_common = allowed_identity
    return requested_top == root and allowed_top == allowed_root and requested_common == allowed_common


def authorized_zvec_root(raw_root: Any, allowed_roots: frozenset[pathlib.Path]) -> pathlib.Path | None:
    """Return an exact allowlisted path or a linked worktree of one."""
    if not isinstance(raw_root, str) or not raw_root.strip() or not allowed_roots:
        return None
    try:
        requested = pathlib.Path(raw_root).expanduser().resolve(strict=True)
    except OSError:
        return None
    resolved_allowed: list[pathlib.Path] = []
    for allowed in allowed_roots:
        try:
            allowed_real = allowed.resolve(strict=True)
            if requested == allowed_real:
                return requested
            resolved_allowed.append(allowed_real)
        except OSError:
            continue
    if any(is_git_worktree_of(requested, allowed) for allowed in resolved_allowed):
        return requested
    return None
