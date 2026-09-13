import { describe, expect, test } from "bun:test"
import {
  buildNudgeEvent,
  buildEngramToolUseEvent,
  isEngramTool,
  summarizeConversion,
} from "./metrics"

describe("steering metrics", () => {
  test("detects engram tools only", () => {
    expect(isEngramTool("engram_code_search")).toBe(true)
    expect(isEngramTool("mcp_engram_docs_recall")).toBe(true)
    expect(isEngramTool("grep")).toBe(false)
    expect(isEngramTool("bash")).toBe(false)
  })

  test("conversion counts sessions with tool use after nudge", () => {
    const events = [
      buildNudgeEvent("s1", "grep", "TODO", 3),
      buildEngramToolUseEvent("s1", "engram_code_search"),
      buildNudgeEvent("s2", "rg", "foo", 1),
    ]
    const s = summarizeConversion(events)
    expect(s.nudges).toBe(2)
    expect(s.totalSessionsWithNudges).toBe(2)
    expect(s.convertedSessions).toBe(1)
    expect(s.conversionRate).toBe(0.5)
  })

  test("tool use before nudge does not convert", () => {
    const events = [
      buildEngramToolUseEvent("s1", "engram_code_search"),
      buildNudgeEvent("s1", "grep", "TODO", 1),
    ]
    expect(summarizeConversion(events).convertedSessions).toBe(0)
  })

  test("empty log summarizes to zero", () => {
    const s = summarizeConversion([])
    expect(s.nudges).toBe(0)
    expect(s.conversionRate).toBe(0)
  })
})
