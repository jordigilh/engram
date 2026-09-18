import { describe, expect, test } from "bun:test"
import { SubagentGate, type SessionLookup } from "./subagent-gate"

function lookup(sessions: Record<string, { parentID?: string | null }>): SessionLookup {
  return {
    session: {
      get: async ({ path }) => {
        const data = sessions[path.id]
        if (!data) throw new Error("not found")
        return { data }
      },
    },
  }
}

describe("SubagentGate", () => {
  test("does not block the primary session", async () => {
    const gate = new SubagentGate(lookup({ primary: {} }))

    expect(await gate.shouldBlock("primary", "bash")).toBe(false)
  })

  test("blocks unknown sessions and child sessions before Engram", async () => {
    const gate = new SubagentGate(lookup({ child: { parentID: "primary" } }))

    expect(await gate.shouldBlock("child", "bash")).toBe(true)
    expect(await gate.shouldBlock("missing", "read")).toBe(true)
  })

  test("allows Engram tools before unlock", async () => {
    const gate = new SubagentGate(lookup({ child: { parentID: "primary" } }))

    expect(await gate.shouldBlock("child", "engram_docs_recall")).toBe(false)
    expect(await gate.shouldBlock("child", "mcp_engram_docs_recall")).toBe(false)
  })

  test("unlocks only after a successful Engram tool completion", async () => {
    const gate = new SubagentGate(lookup({ child: { parentID: "primary" } }))

    expect(await gate.shouldBlock("child", "read")).toBe(true)
    gate.markSuccessfulEngramCall("child", "engram_docs_recall")
    expect(await gate.shouldBlock("child", "read")).toBe(false)
  })

  test("failed calls leave the session locked", async () => {
    const gate = new SubagentGate(lookup({ child: { parentID: "primary" } }))

    expect(await gate.shouldBlock("child", "bash")).toBe(true)
    expect(await gate.shouldBlock("child", "engram_docs_recall")).toBe(false)
    expect(await gate.shouldBlock("child", "bash")).toBe(true)
  })

  test("clear removes both the classification and unlock state", async () => {
    const gate = new SubagentGate(lookup({ child: { parentID: "primary" } }))

    gate.markSuccessfulEngramCall("child", "engram_docs_recall")
    expect(await gate.shouldBlock("child", "read")).toBe(false)
    gate.clear("child")
    expect(await gate.shouldBlock("child", "read")).toBe(true)
  })

  test("clearAll releases process-local state", async () => {
    const gate = new SubagentGate(lookup({ child: { parentID: "primary" } }))

    gate.markSuccessfulEngramCall("child", "engram_docs_recall")
    gate.clearAll()
    expect(await gate.shouldBlock("child", "read")).toBe(true)
  })
})
