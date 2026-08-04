#!/usr/bin/env python3
"""Merge the Deterministic Correction Enforcement hook pair into a target
repo's .cursor/hooks.json, preserving any existing hook registrations.
Idempotent -- re-running with the same args does not duplicate entries.

Used by hooks/install.sh; not meant to be run standalone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MATCHER = "Write|StrReplace|Shell|EditNotebook"
# Outer Cursor-enforced timeout for the whole preToolUse hook process. Must
# exceed the enforcer's own internal watchdog budget (45s, see
# post-plan-hindsight-check.py) with headroom for interpreter startup, or
# Cursor could kill the hook before its own fail-open path gets to run.
ENFORCER_TIMEOUT_S = 60
DETECTOR_TIMEOUT_S = 5


def merge(config: dict, detector_cmd: str, enforcer_cmd: str) -> dict:
    config.setdefault("version", 1)
    config.setdefault("hooks", {})

    before_submit = config["hooks"].setdefault("beforeSubmitPrompt", [])
    if not any(h.get("command") == detector_cmd for h in before_submit):
        before_submit.append({"command": detector_cmd, "timeout": DETECTOR_TIMEOUT_S})

    pre_tool = config["hooks"].setdefault("preToolUse", [])
    if not any(h.get("command") == enforcer_cmd and h.get("matcher") == MATCHER for h in pre_tool):
        pre_tool.append({"matcher": MATCHER, "command": enforcer_cmd, "timeout": ENFORCER_TIMEOUT_S})

    return config


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: _merge_hooks_json.py <hooks.json path> <detector command> <enforcer command>",
            file=sys.stderr,
        )
        return 1

    hooks_json_path = Path(sys.argv[1])
    detector_cmd, enforcer_cmd = sys.argv[2], sys.argv[3]

    config: dict = {"version": 1, "hooks": {}}
    if hooks_json_path.exists():
        try:
            config = json.loads(hooks_json_path.read_text())
        except json.JSONDecodeError:
            print(
                f"error: {hooks_json_path} is not valid JSON -- refusing to overwrite, merge by hand",
                file=sys.stderr,
            )
            return 1

    config = merge(config, detector_cmd, enforcer_cmd)
    hooks_json_path.write_text(json.dumps(config, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
