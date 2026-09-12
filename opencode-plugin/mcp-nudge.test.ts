import { describe, expect, test } from "bun:test"
import { shouldNudgeMcp, buildMcpNudgeMessage } from "./mcp-nudge"

describe("shouldNudgeMcp", () => {
  test("nudges bash engram CLI invocations only", () => {
    expect(shouldNudgeMcp("bash", "engram recall --query foo")).toBe("engram")
    expect(shouldNudgeMcp("bash", "hindsight search --q bar")).toBeNull()
    expect(shouldNudgeMcp("bash", "cocoindex status")).toBeNull()
    expect(shouldNudgeMcp("bash", "serena search")).toBeNull()
  })

  test("ignores non-bash tools, empty commands, and substring matches", () => {
    expect(shouldNudgeMcp("read", "engram recall")).toBeNull()
    expect(shouldNudgeMcp("bash", "")).toBeNull()
    expect(shouldNudgeMcp("bash", "echo hello")).toBeNull()
    expect(shouldNudgeMcp("bash", "my-engram-wrapper run")).toBeNull()
  })

  test("nudge message prefers MCP", () => {
    expect(buildMcpNudgeMessage("engram")).toMatch(/MCP.*over CLI/i)
  })
})
