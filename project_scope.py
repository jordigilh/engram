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

ALLOWED_WORKSPACE_PREFIXES = [
    # Covers kubernaut and every kubernaut-* sibling repo (operator,
    # console-plugin, docs, demo-scenarios, patent, presentation,
    # test-playbooks, v1-2/3/5/6, apifrontend, ...).
    "Users-jgil-go-src-github-com-jordigilh-kubernaut",
    "Users-jgil-go-src-github-com-dcm-project-",
    # Engram's own dev transcripts -- working on Engram itself produces
    # genuine coding-hygiene corrections too, not just kubernaut/dcm work.
    "Users-jgil-go-src-github-com-jordigilh-engram",
]


def is_allowed_workspace(project_dir_name: str) -> bool:
    """True if a Cursor workspace directory name (the basename under
    ~/.cursor/projects/) belongs to an onboarded project."""
    return any(project_dir_name.startswith(prefix) for prefix in ALLOWED_WORKSPACE_PREFIXES)


def transcript_glob_patterns() -> list[str]:
    """CocoIndex PatternFilePathMatcher included_patterns for the
    transcript_app -- one glob per allowed prefix, matching that workspace
    and any sibling repo sharing the same prefix (e.g. kubernaut-operator)."""
    return [f"{prefix}*/agent-transcripts/**/*.jsonl" for prefix in ALLOWED_WORKSPACE_PREFIXES]
