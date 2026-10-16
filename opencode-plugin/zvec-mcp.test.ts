import { describe, expect, test } from "bun:test"
import {
  ZvecMcpClient,
  runZvecMcpIndex,
  type FetchLike,
} from "./zvec-mcp"

function sse(value: unknown, sessionID = "session-1"): Response {
  return new Response(`data: ${JSON.stringify(value)}\n\n`, {
    headers: {
      "content-type": "text/event-stream",
      "mcp-session-id": sessionID,
    },
  })
}

describe("containerized zvec MCP client", () => {
  test("initializes a session and calls the index tool over SSE", async () => {
    const calls: Array<{ method: string; params?: any }> = []
    const fetcher: FetchLike = async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as { method: string; params?: any }
      calls.push(request)
      if (request.method === "initialize") {
        return sse({ jsonrpc: "2.0", id: 1, result: { protocolVersion: "2025-06-18" } })
      }
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      return sse({
        jsonrpc: "2.0",
        id: 2,
        result: { structuredContent: { state: "succeeded" } },
      })
    }

    const client = new ZvecMcpClient({ url: "http://127.0.0.1:7999/mcp", fetch: fetcher })
    await expect(client.callTool("zvec_grep_index", { root: "/worktrees/fix-123", wait: true })).resolves.toEqual({
      structuredContent: { state: "succeeded" },
    })
    expect(calls.map((call) => call.method)).toEqual([
      "initialize",
      "notifications/initialized",
      "tools/call",
    ])
    expect(calls[2]?.params).toEqual({
      name: "zvec_grep_index",
      arguments: { root: "/worktrees/fix-123", wait: true },
    })
  })

  test("surfaces an MCP tool error", async () => {
    const fetcher: FetchLike = async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as { method: string }
      if (request.method === "initialize") return sse({ jsonrpc: "2.0", id: 1, result: {} })
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      return sse({
        jsonrpc: "2.0",
        id: 2,
        result: { isError: true, content: [{ type: "text", text: "workspace busy" }] },
      })
    }
    const client = new ZvecMcpClient({ url: "http://zvec.test/mcp", fetch: fetcher })
    await expect(client.callTool("zvec_grep_index", { root: "/worktrees/fix-123" })).rejects.toThrow("workspace busy")
  })

  test("submits a root-scoped index and rejects non-success states", async () => {
    const calls: Array<{ name: string; arguments: Record<string, unknown> }> = []
    const caller = {
      callTool: async (name: string, arguments_: Record<string, unknown>) => {
        calls.push({ name, arguments: arguments_ })
        return { structuredContent: { state: "failed", error: { message: "index failed" } } }
      },
    }
    await expect(runZvecMcpIndex(caller, "/worktrees/fix-123", "local/test", true)).rejects.toThrow("index failed")
    expect(calls).toEqual([{
      name: "zvec_grep_index",
      arguments: {
        root: "/worktrees/fix-123",
        embedding: "local/test",
        rebuild: true,
        wait: true,
      },
    }])
  })
})
