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

Keep in sync with nightly-learn.py's PROJECT_CONFIGS (used there for a
different purpose -- per-project analytics scoping, i.e. "which onboarded
project does this transcript belong to" -- vs. this module's "is this
transcript in scope for the shared retain pipeline at all").
"""
from __future__ import annotations

# Single source of truth for both "is this workspace in scope at all" and
# "which onboarded project label does it map to" -- added 2026-07-19 after
# finding every contradictions-pending.jsonl entry had project=null (see
# docs/FINDINGS.md), because nothing upstream of pending_queue.append_pending()
# ever resolved a transcript's path back to kubernaut/dcm/engram. Keep this
# dict, not just a bare prefix list, so both concerns can never drift apart.
PROJECT_LABEL_BY_PREFIX = {
    # Covers kubernaut and every kubernaut-* sibling repo (operator,
    # console-plugin, docs, demo-scenarios, patent, presentation,
    # test-playbooks, v1-2/3/5/6, apifrontend, ...).
    "Users-jgil-go-src-github-com-jordigilh-kubernaut": "kubernaut",
    "Users-jgil-go-src-github-com-dcm-project-": "dcm",
    # Engram's own dev transcripts -- working on Engram itself produces
    # genuine coding-hygiene corrections too, not just kubernaut/dcm work.
    "Users-jgil-go-src-github-com-jordigilh-engram": "engram",
}

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
