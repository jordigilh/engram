#!/bin/bash
# beforeSubmitPrompt detector for the Deterministic Correction Enforcement
# hook pair (see docs/findings/2026-08.md for the design history/spikes).
#
# Detects Cursor's fixed auto-continue "implement the plan" message and, on
# match, hands off the newly-confirmed plan's `overview` field + detected
# project to hooks/post-plan-hindsight-check.py (preToolUse enforcer) via a
# per-session marker file. Never blocks -- always returns continue:true.
# A missed/false-negative match just means the enforcer silently doesn't
# fire on this plan (fails open by construction, not by exception handling).

input=$(cat)
prompt=$(echo "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("prompt",""))' 2>/dev/null)

if echo "$prompt" | grep -qF "Implement the plan as specified" && echo "$prompt" | grep -qF "Don't stop until you have completed all the to-dos"; then
  session_id=$(echo "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null)
  workspace_roots=$(echo "$input" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin).get("workspace_roots",[])))' 2>/dev/null)
  python3 "$(dirname "$0")/_write_plan_marker.py" "$session_id" "$workspace_roots" 2>/dev/null
fi

echo '{"continue": true}'
exit 0
