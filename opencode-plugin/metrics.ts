// Pure steering-metric helpers for the hooks spike. Kept dependency-free
// so they're unit-testable; index.ts appends the events to a jsonl log.
//
// Events (one JSON object per line):
//   {kind:"nudge", ts, sessionID, cli, query, hits}
//   {kind:"engram_tool_use", ts, sessionID, tool}
// Conversion = sessions with a nudge followed by an engram tool use.
// Reads like: N nudges → M sessions converted (M/N %).

export interface NudgeEvent {
  kind: "nudge"
  ts: string
  sessionID: string
  cli: string
  query: string
  hits: number
}

export interface EngramToolUseEvent {
  kind: "engram_tool_use"
  ts: string
  sessionID: string
  tool: string
}

export type MetricEvent = NudgeEvent | EngramToolUseEvent

export function buildNudgeEvent(sessionID: string, cli: string, query: string, hits: number): NudgeEvent {
  return { kind: "nudge", ts: new Date().toISOString(), sessionID, cli, query: query.slice(0, 80), hits }
}

export function buildEngramToolUseEvent(sessionID: string, tool: string): EngramToolUseEvent {
  return { kind: "engram_tool_use", ts: new Date().toISOString(), sessionID, tool }
}

export function isEngramTool(tool: string): boolean {
  const t = tool.toLowerCase()
  return t.includes("engram") && !t.includes("engram-plugin")
}

export interface ConversionSummary {
  nudges: number
  convertedSessions: number
  totalSessionsWithNudges: number
  conversionRate: number
}

export function summarizeConversion(events: MetricEvent[]): ConversionSummary {
  const nudgedSessions = new Set<string>()
  const convertedSessions = new Set<string>()
  let nudges = 0
  for (const e of events) {
    if (e.kind === "nudge") {
      nudges++
      if (e.sessionID) nudgedSessions.add(e.sessionID)
    }
  }
  // A session counts as converted when an engram tool use has a nudge
  // earlier in the same session (event order = file order).
  const firstNudgeIdx = new Map<string, number>()
  events.forEach((e, i) => {
    if (e.kind === "nudge" && e.sessionID && !firstNudgeIdx.has(e.sessionID)) {
      firstNudgeIdx.set(e.sessionID, i)
    }
  })
  events.forEach((e, i) => {
    if (e.kind === "engram_tool_use" && e.sessionID) {
      const ni = firstNudgeIdx.get(e.sessionID)
      if (ni !== undefined && ni < i) convertedSessions.add(e.sessionID)
    }
  })
  const total = nudgedSessions.size
  return {
    nudges,
    convertedSessions: convertedSessions.size,
    totalSessionsWithNudges: total,
    conversionRate: total === 0 ? 0 : convertedSessions.size / total,
  }
}
