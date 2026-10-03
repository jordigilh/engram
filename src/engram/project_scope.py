#!/usr/bin/env python3
"""Single source of truth for which Cursor workspaces are allowed to feed the
shared cursor-memory retain pipeline (nightly-learn.py's run_hourly()/
run_nightly(), cocoindex-flows.py's transcript_app).

Added 2026-07-12/13 after discovering the retain path had no project filter
at all: it swept every one of the ~270 Cursor workspaces on this machine
(including totally unrelated repos like koku/insights-onprem,
redhat-developer-rhdh-plugins, and blank "no folder open" sessions) into
cursor-memory, not just the projects Engram has actually been onboarded for.
See docs/FINDINGS.md.

Project labels and workspace prefixes are loaded from the deployment-local
projects.toml. The generic code does not embed a host's checkout layout.
"""
from __future__ import annotations

from engram.project_config import load_all_project_settings  # noqa: E402


def _load_project_label_by_prefix() -> dict[str, str]:
    """Build the workspace allowlist from deployment-local project config."""
    labels: dict[str, str] = {}
    for project, settings in load_all_project_settings().items():
        for prefix in settings.strings("workspace_prefixes"):
            labels[prefix] = project
    return labels


# Public derived values remain available to callers; the source of truth is
# ~/.engram/projects.toml rather than this package.
PROJECT_LABEL_BY_PREFIX = _load_project_label_by_prefix()

ALLOWED_WORKSPACE_PREFIXES = list(PROJECT_LABEL_BY_PREFIX.keys())


def is_allowed_workspace(project_dir_name: str) -> bool:
    """True if a Cursor workspace directory name (the basename under
    ~/.cursor/projects/) belongs to an onboarded project."""
    return any(project_dir_name.startswith(prefix) for prefix in ALLOWED_WORKSPACE_PREFIXES)


def resolve_project_label(project_dir_name: str) -> str | None:
    """Map a Cursor workspace directory name to its onboarded project label
    (kubernaut/dcm/engram), or None if it isn't an onboarded workspace.

    Used to tag per-project data (e.g. contradictions-pending.jsonl entries)
    at write time so downstream reporting can actually filter by project,
    instead of every entry defaulting to project=null."""
    for prefix, label in PROJECT_LABEL_BY_PREFIX.items():
        if project_dir_name.startswith(prefix):
            return label
    return None


def transcript_glob_patterns() -> list[str]:
    """CocoIndex PatternFilePathMatcher included_patterns for the
    transcript_app -- one glob per allowed prefix, matching that workspace
    and any sibling repo sharing the same prefix (e.g. kubernaut-operator)."""
    return [f"{prefix}*/agent-transcripts/**/*.jsonl" for prefix in ALLOWED_WORKSPACE_PREFIXES]
