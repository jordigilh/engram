#!/usr/bin/env python3
"""preToolUse enforcer for the Deterministic Correction Enforcement hook
pair. Paired with hooks/detect-plan-kickoff.sh (beforeSubmitPrompt
detector). Full design history and spike evidence: docs/findings/2026-08.md
and the "Deterministic Correction Enforcement" plan.

Consumes the per-session marker the detector wrote (if any) on the FIRST
tool call matching this hook's .cursor/hooks.json matcher (mutating tools:
Write|StrReplace|Shell|EditNotebook) after a plan is confirmed, runs a real
Hindsight contradiction check against the plan's `overview` text in a
separately-killable child process (hard wall-clock watchdog --
recall()/check_contradiction()'s own retry loops are uncapped in aggregate),
and hard-blocks (permission: deny + user_message -- NOT agent_message,
confirmed broken on Cursor 3.14.7) only on a genuine contradiction.

The marker is deleted immediately, before the check runs, deliberately:
  - a crash mid-check never leaves a stale marker to be silently
    re-consumed or stuck
  - a deny is a one-time speed bump, not a permanent lock -- the model sees
    the explanation once; a retry (same or revised action) goes through
    cleanly via the "no marker" fast path. This matters because resolve()
    detects semantic conflict, not intent -- a plan that deliberately,
    correctly supersedes a stale convention looks identical to it as one
    that violates a still-valid one (a real example was found preflighting
    against historical plans, see docs/findings/2026-08.md).

Known gap: a Task subagent's tool calls carry a different session_id than
its parent conversation (confirmed empirically), so a plan whose
implementation is delegated to a subagent gets zero coverage here -- not
circumvention, just an unmatched marker lookup. Accepted scope limit for
this pass.

Fails open on any error, timeout, or missing marker: {"permission":
"allow"}. Never raises -- an uncaught exception here would silently block
real work with no explanation, which is worse than skipping the check.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MARKER_DIR = Path.home() / ".cache" / "engram-hooks"
LOG_PATH = Path.home() / ".hindsight" / "logs" / "post-plan-hindsight-check.jsonl"
# Measured empirically at 13-17s wall-clock for a *successful* real recall()
# + check_contradiction() round trip (venv Python startup + one network hop
# to hindsight-api + one Vertex AI call) -- see docs/findings/2026-08.md.
# 15s would silently fail-open on roughly half of all genuine checks, not
# just the actually-slow/retrying ones. 45s gives real calls comfortable
# headroom while still bounding the worst case (recall()'s and
# check_contradiction()'s own retry loops are uncapped in aggregate).
WATCHDOG_BUDGET_S = 45
WORKER = Path(__file__).resolve().parent / "_hindsight_check_worker.py"


def allow() -> None:
    print(json.dumps({"permission": "allow"}))


def deny(user_message: str) -> None:
    print(json.dumps({"permission": "deny", "user_message": user_message}))


def log(entry: dict) -> None:
    """Best-effort audit trail -- every check outcome (clean/denied/timeout/
    error) is logged so the false-positive rate is directly observable
    rather than inferred later from reflect() trends alone."""
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
        allow()
        return 0

    session_id = payload.get("session_id", "")
    if not session_id:
        allow()
        return 0

    marker_path = MARKER_DIR / f"plan-kickoff-{session_id}.json"
    if not marker_path.exists():
        allow()
        return 0

    try:
        marker = json.loads(marker_path.read_text())
    except (OSError, json.JSONDecodeError):
        marker = None

    try:
        marker_path.unlink()
    except OSError:
        pass

    if not marker or not marker.get("overview"):
        allow()
        return 0

    overview = marker["overview"]
    project = marker.get("project")

    t0 = time.time()
    proc = None
    try:
        proc = subprocess.run(
            [sys.executable, str(WORKER)],
            input=json.dumps({"overview": overview, "project": project}),
            capture_output=True, text=True, timeout=WATCHDOG_BUDGET_S,
        )
        elapsed = time.time() - t0
        result = json.loads(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None
    except subprocess.TimeoutExpired:
        log({"project": project, "outcome": "timeout", "budget_s": WATCHDOG_BUDGET_S})
        allow()
        return 0
    except (OSError, json.JSONDecodeError):
        elapsed = time.time() - t0
        result = None

    if not result:
        log({
            "project": project, "outcome": "worker_failed",
            "stderr": (proc.stderr[:500] if proc else ""), "elapsed_s": round(elapsed, 1),
        })
        allow()
        return 0

    action = result.get("action", "retain")
    confidence = result.get("confidence", 0.0)
    explanation = result.get("explanation", "")

    if action in ("auto_resolved", "queued"):
        log({
            "project": project, "outcome": "denied", "action": action, "confidence": confidence,
            "explanation": explanation[:500], "elapsed_s": round(elapsed, 1),
        })
        deny(
            f"This plan's stated approach conflicts with an existing convention "
            f"(confidence {confidence:.2f}): {explanation} "
            f"If this is an intentional, deliberate change, simply retry the same "
            f"action -- this check only fires once per plan."
        )
        return 0

    log({"project": project, "outcome": "clean", "elapsed_s": round(elapsed, 1)})
    allow()
    return 0


def run_main_safely() -> int:
    """Last-resort fail-open guard: no matter what goes wrong inside main()
    (including bugs never hit by the fail-open branches above), the hook
    must still emit valid allow JSON rather than blocking real work with an
    unhandled traceback and no output."""
    try:
        return main()
    except Exception as e:  # noqa: BLE001
        log({"outcome": "crash", "error": str(e)})
        print(json.dumps({"permission": "allow"}))
        return 0


if __name__ == "__main__":
    sys.exit(run_main_safely())
