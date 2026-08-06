#!/usr/bin/env python3
"""Helper for hooks/detect-plan-kickoff.sh: find the newest confirmed plan
file, extract its `overview` frontmatter field, detect the project and repo
name from workspace_roots, and write a per-session marker consumed by
hooks/post-plan-hindsight-check.py (preToolUse enforcer) and, if a checklist
file exists for this repo, hooks/post-plan-checklist-reminder.py (postToolUse
reminder) on the first matched mutating tool call of the implementation.

Deliberately extracts only the `overview` field, not the full plan body --
hindsight-api's /recall endpoint hard-caps queries at 500 tokens, and a
full/near-full plan excerpt reliably exceeds that (confirmed empirically,
see docs/findings/2026-08.md), which would make the whole check a silent,
permanent no-op. `overview` is 1-3 sentences on every plan file inspected
and is also the semantically right level of granularity to check (stated
intent/approach, not implementation detail).

Never raises -- any failure here should silently no-op. The detector fails
open by construction: a missing marker just means the enforcer doesn't fire
for this plan, not that anything breaks.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLANS_DIR = Path.home() / ".cursor" / "plans"
MARKER_DIR = Path.home() / ".cache" / "engram-hooks"
OVERVIEW_RE = re.compile(r"^overview:\s*(.+?)(?=\ntodos:|\n---)", re.DOTALL | re.MULTILINE)

# Lightweight path-prefix check, deliberately not importing engram's own
# project_scope.py: this script runs from inside other repos' .cursor/hooks
# (kubernaut, dcm-project/*), which don't have engram's codebase checked out.
PROJECT_PATH_MARKERS = {
    "dcm": "/dcm-project/",
    "kubernaut": "/jordigilh/kubernaut",
}


def detect_project(workspace_roots: list[str]) -> str | None:
    for root in workspace_roots:
        for project, marker in PROJECT_PATH_MARKERS.items():
            if marker in root:
                return project
    return None


def detect_repo_name(workspace_roots: list[str]) -> str | None:
    """Basename of the first non-empty workspace root, used by
    hooks/post-plan-checklist-reminder.py to look up a per-repo checklist
    file (review-checklists/<repo>.md). Deliberately generic (not tied to
    the dcm/kubernaut project-family labels above) -- the checklist
    mechanism scopes itself by file existence, not by this function."""
    for root in workspace_roots:
        name = Path(root.rstrip("/")).name
        if name:
            return name
    return None


def main() -> int:
    if len(sys.argv) < 3:
        return 0
    session_id = sys.argv[1]
    workspace_roots = sys.argv[2].split() if sys.argv[2] else []
    if not session_id:
        return 0

    try:
        plan_files = sorted(PLANS_DIR.glob("*.plan.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    if not plan_files:
        return 0

    try:
        text = plan_files[0].read_text(errors="replace")
    except OSError:
        return 0

    m = OVERVIEW_RE.search(text)
    if not m:
        return 0
    overview = m.group(1).strip().strip('"')
    if not overview:
        return 0

    project = detect_project(workspace_roots)
    repo = detect_repo_name(workspace_roots)

    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker_path = MARKER_DIR / f"plan-kickoff-{session_id}.json"
        marker_path.write_text(json.dumps({
            "overview": overview,
            "project": project,
            "repo": repo,
            "plan_file": str(plan_files[0]),
        }))
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
