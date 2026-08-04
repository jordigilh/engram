#!/usr/bin/env python3
"""Child worker for hooks/post-plan-hindsight-check.py, run under a hard
subprocess timeout by the parent (see its module docstring). Does the real
recall() + contradiction_resolution.resolve() call, which can legitimately
take 5-10s and has uncapped retry loops in the worst case -- isolated here
so the parent can kill it cleanly on timeout (confirmed empirically to leave
no orphaned processes, see docs/findings/2026-08.md) without the preToolUse
hook itself hanging.

Reads {"overview": str, "project": str|null} from stdin, prints
{"action": "retain"|"auto_resolved"|"queued", "confidence": float,
"explanation": str} to stdout. Never raises: any internal failure degrades
to action="retain" so the parent's fail-open path is exercised, not a crash.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def load_config_env() -> None:
    """Source ~/.hindsight/config.env's KEY=VALUE lines into os.environ
    without overwriting anything the hook's own environment already set."""
    config_path = Path.home() / ".hindsight" / "config.env"
    if not config_path.exists():
        return
    for line in config_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def fix_credentials_path() -> None:
    """config.env's GOOGLE_APPLICATION_CREDENTIALS points at a
    container-only path (/tmp/keys/adc.json) that doesn't exist in an
    interactive-shell hook execution context -- override to the real local
    gcloud ADC file if it exists and the configured path doesn't (confirmed
    necessary empirically, see docs/findings/2026-08.md)."""
    configured = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    local_adc = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if not (configured and os.path.exists(configured)) and os.path.exists(local_adc):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = local_adc


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        overview = payload.get("overview", "")
        project = payload.get("project")
        if not overview:
            print(json.dumps({"action": "retain", "confidence": 0.0, "explanation": ""}))
            return 0

        load_config_env()
        fix_credentials_path()
        # ~/.hindsight/contradiction_resolution.py is a symlink to engram's
        # real module (see docs/INSTALL.md step 9/16); this makes the
        # import work from any repo's working directory, not just engram's.
        sys.path.insert(0, os.path.expanduser("~/.hindsight"))
        from contradiction_resolution import resolve  # noqa: E402

        res = resolve("cursor-memory", overview, project=project)
        print(json.dumps({"action": res.action, "confidence": res.confidence, "explanation": res.explanation}))
        return 0
    except Exception as e:  # noqa: BLE001 -- worker must always emit valid JSON
        print(json.dumps({"action": "retain", "confidence": 0.0, "explanation": f"worker error: {e}"}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
