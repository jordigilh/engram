#!/bin/bash
# Log all MCP tool calls with hit/miss classification.
# Triggered by afterMCPExecution hook event.
# Writes to ~/.hindsight/logs/mcp-calls.jsonl

LOG_DIR="$HOME/.hindsight/logs"
LOG_FILE="$LOG_DIR/mcp-calls.jsonl"
mkdir -p "$LOG_DIR"

input=$(cat)

server=$(echo "$input" | jq -r '.mcp_server_name // "unknown"')
tool=$(echo "$input" | jq -r '.tool_name // "unknown"')
duration=$(echo "$input" | jq -r '.duration // 0')

# result_json is a STRING containing JSON (double-encoded by Cursor)
result_raw=$(echo "$input" | jq -r '.result_json // ""')

hit="false"
is_error="false"
result_chars=0

if [ -n "$result_raw" ] && [ "$result_raw" != "{}" ]; then
  # Parse the stringified JSON
  is_error=$(echo "$result_raw" | jq -r '.isError // false' 2>/dev/null)
  [ "$is_error" != "true" ] && is_error="false"

  # Cursor truncates content text in hook payloads, so we infer hit from
  # structure: if content array exists with a text entry and no error, it's a hit
  has_content=$(echo "$result_raw" | jq -r '.content[0].type // empty' 2>/dev/null)
  if [ "$has_content" = "text" ] && [ "$is_error" != "true" ]; then
    hit="true"
  fi

  # Get whatever char count is available (may be 0 if truncated)
  result_chars=$(echo "$result_raw" | jq -r '.content[0].text // empty' 2>/dev/null | wc -c | tr -d ' ')
fi

ts=$(date -u +"%Y-%m-%dT%H:%M:%S")

printf '{"ts":"%s","server":"%s","tool":"%s","hit":%s,"result_chars":%d,"duration_ms":%.0f,"is_error":%s}\n' \
  "$ts" "$server" "$tool" "$hit" "$result_chars" "${duration%.*}" "$is_error" >> "$LOG_FILE"

exit 0
