#!/bin/bash
# Installs the Deterministic Correction Enforcement hook pair (detector +
# enforcer) into a target repo's .cursor/hooks.json. Safe to re-run --
# merges into any existing hooks.json rather than overwriting it (see
# hooks/_merge_hooks_json.py).
#
# Usage: hooks/install.sh [target-repo-path]
#   target-repo-path defaults to the current working directory.
#
# What this does:
#   1. Symlinks the four hook scripts into the shared ~/.hindsight/hooks/
#      location (single source of truth, same pattern as chunking.py's
#      symlink into ~/.hindsight/ -- see docs/INSTALL.md step 16), so every
#      onboarded repo's .cursor/hooks.json can reference one stable path
#      regardless of where engram itself happens to be checked out.
#   2. Registers a beforeSubmitPrompt detector and a preToolUse enforcer in
#      the target repo's .cursor/hooks.json.
#
# See docs/NEW_PROJECT_SETUP.md and docs/findings/2026-08.md for the full
# design/rollout rationale, including why the enforcer must run under
# ~/.hindsight/venv's Python (system python3 lacks litellm/vertexai).
set -euo pipefail

ENGRAM_HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_REPO="${1:-$(pwd)}"
SHARED_HOOKS_DIR="$HOME/.hindsight/hooks"
VENV_PYTHON="$HOME/.hindsight/venv/bin/python3"

if [ ! -d "$TARGET_REPO/.git" ]; then
  echo "error: $TARGET_REPO does not look like a git repo root (no .git dir)" >&2
  exit 1
fi

if [ ! -x "$VENV_PYTHON" ]; then
  echo "error: $VENV_PYTHON not found -- the enforcer needs engram's Hindsight venv (litellm, vertexai) to run" >&2
  exit 1
fi

mkdir -p "$SHARED_HOOKS_DIR"
for f in detect-plan-kickoff.sh post-plan-hindsight-check.py _write_plan_marker.py _hindsight_check_worker.py; do
  ln -sf "$ENGRAM_HOOKS_DIR/$f" "$SHARED_HOOKS_DIR/$f"
done

DETECTOR_CMD="$SHARED_HOOKS_DIR/detect-plan-kickoff.sh"
ENFORCER_CMD="$VENV_PYTHON $SHARED_HOOKS_DIR/post-plan-hindsight-check.py"

mkdir -p "$TARGET_REPO/.cursor"
python3 "$ENGRAM_HOOKS_DIR/_merge_hooks_json.py" \
  "$TARGET_REPO/.cursor/hooks.json" \
  "$DETECTOR_CMD" \
  "$ENFORCER_CMD"

echo "Installed Deterministic Correction Enforcement hooks into $TARGET_REPO/.cursor/hooks.json"
echo "  beforeSubmitPrompt -> $DETECTOR_CMD"
echo "  preToolUse (Write|StrReplace|Shell|EditNotebook) -> $ENFORCER_CMD"
echo "Restart Cursor (or reload the window) in $TARGET_REPO for hooks.json changes to take effect."
