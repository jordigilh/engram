#!/usr/bin/env python3
"""postToolUse reminder hook, the third member of the Deterministic
Correction Enforcement hook family (see hooks/detect-plan-kickoff.sh and
hooks/post-plan-hindsight-check.py; full design history in
docs/findings/2026-08.md and the "Hook-delivered PR review checklist" plan).

Fires on the same matcher (Write|StrReplace|Shell|EditNotebook) as the
preToolUse enforcer, on the same tool call, right after it (Cursor fires
postToolUse after the tool executes, and never at all if preToolUse denied
it -- confirmed empirically). Consumes the per-session marker written by
hooks/_write_plan_marker.py, and if a checklist file exists for the marker's
`repo` at ~/.hindsight/review-checklists/<repo>.md, injects it via
`additional_context` -- the one hook-output field confirmed to reliably
reach the model as a `system_reminder` (see docs/findings/2026-08.md).

This is deliberately independent of whatever the enforcer decided: it
re-reads the marker and re-checks file existence itself rather than trusting
a flag from the enforcer, so it still works even if the enforcer crashed
before reaching its own has_checklist check (post-plan-hindsight-check.py's
crash handler intentionally never deletes the marker either).

Content is sourced from engram/hooks/review-checklists/<repo>.md, a
git-tracked file symlinked (by hooks/install.sh) into
~/.hindsight/review-checklists/. Git history is the real integrity control
here (any tampering shows up in `git diff`/`git log`) -- the
is_safe_checklist_content() sanity check below is a cheap secondary layer on
top of that, not a substitute for it, and it also catches accidental
corruption, not just malicious edits.

Fails open (empty `{}`, i.e. no advisory injected) on any error, missing
marker, missing checklist file, or content that fails the sanity check.
Never raises.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

MARKER_DIR = Path.home() / ".cache" / "engram-hooks"
REVIEW_CHECKLISTS_DIR = Path.home() / ".hindsight" / "review-checklists"
LOG_PATH = Path.home() / ".hindsight" / "logs" / "post-plan-checklist-reminder.jsonl"

MAX_CHECKLIST_CHARS = 3000
# Best-effort text patterns, not a cryptographic guarantee -- see module
# docstring. Flags the checklist content, not the model's own output.
_SUSPICIOUS_PATTERNS = [
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\$\("),
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"^\s*(system|assistant)\s*:", re.IGNORECASE | re.MULTILINE),
]


def is_safe_checklist_content(text: str) -> bool:
    if not text or not text.strip():
        return False
    if len(text) > MAX_CHECKLIST_CHARS:
        return False
    return not any(p.search(text) for p in _SUSPICIOUS_PATTERNS)


def log(entry: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({}))
        return 0

    session_id = payload.get("session_id", "")
    if not session_id:
        print(json.dumps({}))
        return 0

    marker_path = MARKER_DIR / f"plan-kickoff-{session_id}.json"
    if not marker_path.exists():
        print(json.dumps({}))
        return 0

    try:
        marker = json.loads(marker_path.read_text())
    except (OSError, json.JSONDecodeError):
        marker = None

    # Final cleanup point in the hand-off chain -- delete unconditionally,
    # regardless of what we find below, so a marker never outlives this hook.
    try:
        marker_path.unlink()
    except OSError:
        pass

    repo = marker.get("repo") if marker else None
    if not repo:
        log({"outcome": "no_repo"})
        print(json.dumps({}))
        return 0

    checklist_path = REVIEW_CHECKLISTS_DIR / f"{repo}.md"
    if not checklist_path.exists():
        log({"repo": repo, "outcome": "no_checklist_file"})
        print(json.dumps({}))
        return 0

    try:
        content = checklist_path.read_text(errors="replace")
    except OSError:
        log({"repo": repo, "outcome": "read_error"})
        print(json.dumps({}))
        return 0

    if not is_safe_checklist_content(content):
        log({"repo": repo, "outcome": "sanity_check_failed", "chars": len(content)})
        print(json.dumps({}))
        return 0

    log({"repo": repo, "outcome": "injected", "chars": len(content)})
    context = (
        f"Pre-PR review checklist for {repo} -- self-check these before "
        f"opening or updating a PR, to catch them before a reviewer has to:\n\n"
        f"{content.strip()}"
    )
    print(json.dumps({"additional_context": context}))
    return 0


def run_main_safely() -> int:
    """Last-resort fail-open guard, matching the pattern in
    post-plan-hindsight-check.py: no bug here should ever surface as a
    blocked tool call or an unhandled traceback -- just no advisory shown."""
    try:
        return main()
    except Exception as e:  # noqa: BLE001
        log({"outcome": "crash", "error": str(e)})
        print(json.dumps({}))
        return 0


if __name__ == "__main__":
    sys.exit(run_main_safely())
