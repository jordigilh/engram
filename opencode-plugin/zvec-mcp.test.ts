import { describe, expect, test } from "bun:test"
import {
  ZvecMcpClient,
  runZvecMcpIndex,
  zvecMcpErrorCode,
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

  test("reinitializes once after the upstream session expires", async () => {
    const calls: Array<{ method: string; session: string | null }> = []
    let initializeCount = 0
    let toolCallCount = 0
    const fetcher: FetchLike = async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as { method: string }
      const headers = init?.headers as Record<string, string> | undefined
      calls.push({
        method: request.method,
        session: headers?.["Mcp-Session-Id"] || headers?.["mcp-session-id"] || null,
      })
      if (request.method === "initialize") {
        initializeCount += 1
        return new Response(JSON.stringify({ jsonrpc: "2.0", id: initializeCount, result: {} }), {
          headers: {
            "content-type": "application/json",
            "mcp-session-id": `session-${initializeCount}`,
          },
        })
      }
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      toolCallCount += 1
      if (toolCallCount === 1) return new Response("Not Found: Session not found", { status: 404 })
      return new Response(JSON.stringify({
        jsonrpc: "2.0",
        id: 2,
        result: { structuredContent: { state: "succeeded" } },
      }), { headers: { "content-type": "application/json" } })
    }

    const client = new ZvecMcpClient({ url: "http://zvec.test/mcp", fetch: fetcher })
    await expect(client.callTool("zvec_grep_index", { root: "/worktrees/fix-123" })).resolves.toEqual({
      structuredContent: { state: "succeeded" },
    })
    expect(calls.map((call) => call.method)).toEqual([
      "initialize",
      "notifications/initialized",
      "tools/call",
      "initialize",
      "notifications/initialized",
      "tools/call",
    ])
    expect(calls[2]?.session).toBe("session-1")
    expect(calls[5]?.session).toBe("session-2")
  })

  test("recovers when the initialization notification loses the session", async () => {
    let initializeCount = 0
    let notificationCount = 0
    const calls: string[] = []
    const fetcher: FetchLike = async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as { method: string }
      calls.push(request.method)
      if (request.method === "initialize") {
        initializeCount += 1
        return new Response(JSON.stringify({ jsonrpc: "2.0", id: initializeCount, result: {} }), {
          headers: { "content-type": "application/json", "mcp-session-id": `session-${initializeCount}` },
        })
      }
      if (request.method === "notifications/initialized") {
        notificationCount += 1
        if (notificationCount === 1) return new Response("Not Found: Session not found", { status: 404 })
        return new Response(null, { status: 202 })
      }
      return new Response(JSON.stringify({
        jsonrpc: "2.0",
        id: 3,
        result: { structuredContent: { state: "succeeded" } },
      }), { headers: { "content-type": "application/json" } })
    }

    const client = new ZvecMcpClient({ url: "http://zvec.test/mcp", fetch: fetcher })
    await expect(client.callTool("zvec_grep_index", { root: "/worktrees/fix-123" })).resolves.toEqual({
      structuredContent: { state: "succeeded" },
    })
    expect(initializeCount).toBe(2)
    expect(calls).toEqual([
      "initialize",
      "notifications/initialized",
      "initialize",
      "notifications/initialized",
      "tools/call",
    ])
  })

  test("does not retry an unrelated HTTP 404", async () => {
    let initializeCount = 0
    let toolCallCount = 0
    const fetcher: FetchLike = async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as { method: string }
      if (request.method === "initialize") {
        initializeCount += 1
        return new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: {} }), {
          headers: { "content-type": "application/json", "mcp-session-id": "session-1" },
        })
      }
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      toolCallCount += 1
      return new Response("Not Found: workspace does not exist", { status: 404 })
    }

    const client = new ZvecMcpClient({ url: "http://zvec.test/mcp", fetch: fetcher })
    await expect(client.callTool("zvec_grep_index", { root: "/missing" })).rejects.toThrow(
      "zvec-grep MCP HTTP 404: Not Found: workspace does not exist",
    )
    expect(initializeCount).toBe(1)
    expect(toolCallCount).toBe(1)
  })

  test("shares one recovery when concurrent calls observe the same expired session", async () => {
    let initializeCount = 0
    let toolCallCount = 0
    let releaseInitialCalls: () => void = () => {}
    const initialCalls = new Promise<void>((resolve) => {
      releaseInitialCalls = resolve
    })
    const fetcher: FetchLike = async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as { method: string }
      if (request.method === "initialize") {
        initializeCount += 1
        return new Response(JSON.stringify({ jsonrpc: "2.0", id: initializeCount, result: {} }), {
          headers: { "content-type": "application/json", "mcp-session-id": `session-${initializeCount}` },
        })
      }
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      toolCallCount += 1
      if (toolCallCount === 2) releaseInitialCalls()
      if (toolCallCount <= 2) {
        await initialCalls
        return new Response("Not Found: Session not found", { status: 404 })
      }
      return new Response(JSON.stringify({
        jsonrpc: "2.0",
        id: toolCallCount,
        result: { structuredContent: { state: "succeeded" } },
      }), { headers: { "content-type": "application/json" } })
    }

    const client = new ZvecMcpClient({ url: "http://zvec.test/mcp", fetch: fetcher })
    await expect(Promise.all([
      client.callTool("zvec_grep_index", { root: "/worktrees/a" }),
      client.callTool("zvec_grep_index", { root: "/worktrees/b" }),
    ])).resolves.toEqual([
      { structuredContent: { state: "succeeded" } },
      { structuredContent: { state: "succeeded" } },
    ])
    expect(initializeCount).toBe(2)
    expect(toolCallCount).toBe(4)
  })

  test("close prevents an in-flight recovery from resurrecting the client", async () => {
    let initializeCount = 0
    let resolveRecoveryInitialize: ((response: Response) => void) | undefined
    let recoveryStarted: () => void = () => {}
    const recoveryStartedPromise = new Promise<void>((resolve) => {
      recoveryStarted = resolve
    })
    const fetcher: FetchLike = async (_input, init) => {
      const request = JSON.parse(String(init?.body || "{}")) as { method: string }
      if (request.method === "initialize") {
        initializeCount += 1
        if (initializeCount === 2) {
          recoveryStarted()
          return new Promise<Response>((resolve) => {
            resolveRecoveryInitialize = resolve
          })
        }
        return new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: {} }), {
          headers: { "content-type": "application/json", "mcp-session-id": "session-1" },
        })
      }
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      if (init?.method === "DELETE") return new Response(null, { status: 200 })
      return new Response("Not Found: Session not found", { status: 404 })
    }

    const client = new ZvecMcpClient({ url: "http://zvec.test/mcp", fetch: fetcher })
    const operation = client.callTool("zvec_grep_index", { root: "/worktrees/fix-123" })
    await recoveryStartedPromise
    await client.close()
    resolveRecoveryInitialize?.(new Response(JSON.stringify({ jsonrpc: "2.0", id: 2, result: {} }), {
      headers: { "content-type": "application/json", "mcp-session-id": "session-2" },
    }))
    await expect(operation).rejects.toThrow("closed")
  })

  test("stops after one session recovery retry", async () => {
    let initializeCount = 0
    let toolCallCount = 0
    const fetcher: FetchLike = async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as { method: string }
      if (request.method === "initialize") {
        initializeCount += 1
        return new Response(JSON.stringify({ jsonrpc: "2.0", id: initializeCount, result: {} }), {
          headers: { "content-type": "application/json", "mcp-session-id": `session-${initializeCount}` },
        })
      }
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      toolCallCount += 1
      return new Response("Not Found: Session not found", { status: 404 })
    }

    const client = new ZvecMcpClient({ url: "http://zvec.test/mcp", fetch: fetcher })
    let failure: unknown
    try {
      await client.callTool("zvec_grep_index", { root: "/worktrees/fix-123" })
    } catch (error) {
      failure = error
    }
    expect(failure).toBeDefined()
    expect((failure as Error).message).toContain("zvec-grep MCP session lost")
    expect(zvecMcpErrorCode(failure)).toBe("MCP_SESSION_LOST")
    expect(initializeCount).toBe(2)
    expect(toolCallCount).toBe(2)
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
