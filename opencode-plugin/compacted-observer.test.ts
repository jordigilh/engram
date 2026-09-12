import { describe, expect, test } from "bun:test"
import { buildCompactedLog } from "./compacted-observer"

describe("buildCompactedLog", () => {
  test("logs session.compacted with session id", () => {
    const line = buildCompactedLog("session.compacted", "ses_123")
    expect(line).toContain("ses_123")
    expect(line).toMatch(/compacted/i)
  })

  test("ignores unrelated events and empty session", () => {
    expect(buildCompactedLog("session.idle", "ses_123")).toBeNull()
    expect(buildCompactedLog("session.compacted", "")).toBeNull()
  })
})
