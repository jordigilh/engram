// Pure helper for the post-compaction observer. Kept dependency-free so
// it's directly unit-testable; index.ts wires it to the V2 event stream for
// `session.compacted`.
//
// Returns the log line to emit, or null when the event is not a compaction
// we care about. Keeping it pure avoids false positives from unrelated
// session events.

export function buildCompactedLog(eventType: string, sessionID: string): string | null {
  if (eventType !== "session.compacted") return null
  if (!sessionID) return null
  return `[engram-plugin] session compacted: ${sessionID} — methodology recall re-applied via context + compaction hooks`
}
