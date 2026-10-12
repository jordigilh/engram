import { describe, expect, test } from "bun:test"
import {
  ensureEngramSessionReady,
  engramMcpStatus,
  isEngramMcpConnected,
  waitForEngramMcp,
} from "./session-readiness"

describe("session readiness", () => {
  test("recognizes the connected Engram MCP server", () => {
    const result = { data: [{ name: "engram", status: "connected" }] }
    expect(engramMcpStatus(result)).toBe("connected")
    expect(isEngramMcpConnected(result)).toBe(true)
  })

  test("recognizes OpenCode's nested MCP status shape", () => {
    const result = { data: [{ name: "engram", status: { status: "connected" } }] }
    expect(engramMcpStatus(result)).toBe("connected")
    expect(isEngramMcpConnected(result)).toBe(true)
  })

  test("does not treat another MCP server as Engram availability", () => {
    const result = { data: [{ name: "serena", status: "connected" }] }
    expect(engramMcpStatus(result)).toBe("missing")
    expect(isEngramMcpConnected(result)).toBe(false)
  })

  test("waits for a pending MCP connection", async () => {
    let calls = 0
    await waitForEngramMcp({
      listMcp: async () => {
        calls += 1
        return { data: [{ name: "engram", status: calls === 1 ? "pending" : "connected" }] }
      },
      timeoutMs: 100,
      pollIntervalMs: 1,
    })
    expect(calls).toBe(2)
  })

  test("blocks readiness when MCP never connects", async () => {
    await expect(
      waitForEngramMcp({
        listMcp: async () => ({ data: [{ name: "engram", status: "failed" }] }),
        timeoutMs: 0,
      }),
    ).rejects.toThrow("Engram MCP did not become available")
  })

  test("runs indexing only after Engram is connected", async () => {
    const order: string[] = []
    await ensureEngramSessionReady({
      listMcp: async () => {
        order.push("mcp")
        return { data: [{ name: "engram", status: "connected" }] }
      },
      ensureIndex: async () => {
        order.push("index")
      },
    })
    expect(order).toEqual(["mcp", "index"])
  })
})
