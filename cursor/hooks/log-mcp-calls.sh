#!/bin/bash
# Log all MCP tool calls with hit/miss classification.
# Triggered by afterMCPExecution hook event.
# Writes to ~/.hindsight/logs/mcp-calls.jsonl

LOG_DIR="$HOME/.hindsight/logs"
LOG_FILE="$LOG_DIR/mcp-calls.jsonl"
mkdir -p "$LOG_DIR"

input=$(cat)

server=$(echo "$input" | jq -r '.serverName // .server_name // "unknown"')
tool=$(echo "$input" | jq -r '.toolName // .tool_name // "unknown"')
result=$(echo "$input" | jq -r '.result // empty')

result_chars=0
hit="false"

if [ -n "$result" ]; then
  result_chars=$(echo "$result" | wc -c | tr -d ' ')
  if [ "$result_chars" -gt 10 ]; then
    hit="true"
  fi
fi

is_error=$(echo "$input" | jq -r '.isError // .is_error // false')
if [ "$is_error" = "true" ]; then
  hit="false"
fi

ts=$(date -u +"%Y-%m-%dT%H:%M:%S")

printf '{"ts":"%s","server":"%s","tool":"%s","hit":%s,"result_chars":%d,"is_error":%s}\n' \
  "$ts" "$server" "$tool" "$hit" "$result_chars" "$is_error" >> "$LOG_FILE"

exit 0
