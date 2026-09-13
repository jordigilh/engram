// Reads the steering-metric jsonl log and prints nudge→MCP conversion.
// Usage: bun opencode-plugin/metrics-report.ts [--path ~/.engram/logs/opencode-nudges.jsonl]
import { readFileSync, existsSync } from "node:fs"
import { homedir } from "node:os"
import { summarizeConversion, type MetricEvent } from "./metrics"

const argIdx = process.argv.indexOf("--path")
const rawPath = argIdx >= 0 ? process.argv[argIdx + 1] : undefined
const path =
  process.env["ENGRAM_METRICS_PATH"] ||
  rawPath ||
  `${homedir()}/.engram/logs/opencode-nudges.jsonl`

if (!existsSync(path)) {
  console.log(`No metrics yet at ${path} (0 nudges logged).`)
  process.exit(0)
}

const events: MetricEvent[] = []
for (const line of readFileSync(path, "utf-8").split("\n")) {
  const t = line.trim()
  if (!t) continue
  try {
    const e = JSON.parse(t)
    if (e && (e.kind === "nudge" || e.kind === "engram_tool_use")) events.push(e)
  } catch {
    continue
  }
}

const s = summarizeConversion(events)
console.log(`Metrics: ${path}`)
console.log(`Nudges: ${s.nudges} across ${s.totalSessionsWithNudges} session(s)`)
console.log(
  `Converted: ${s.convertedSessions} session(s) used Engram MCP after a nudge ` +
    `(${(s.conversionRate * 100).toFixed(1)}%)`,
)
