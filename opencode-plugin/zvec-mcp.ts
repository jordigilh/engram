import * as path from "node:path"

const MCP_PROTOCOL_VERSION = "2025-06-18"
const DEFAULT_REQUEST_TIMEOUT_MS = 60 * 60 * 1000
export const DEFAULT_ZVEC_MCP_URL = "http://127.0.0.1:7999/mcp"

export type FetchLike = typeof fetch

export interface ZvecMcpToolResult {
  isError?: boolean
  structuredContent?: unknown
  content?: unknown
}

interface JsonRpcMessage {
  result?: ZvecMcpToolResult
  error?: { code?: number; message?: string; data?: unknown }
}

export interface ZvecMcpToolCaller {
  callTool(name: string, arguments_: Record<string, unknown>): Promise<ZvecMcpToolResult>
}

export function resolveZvecMcpUrl(configured?: string): string {
  const value = configured?.trim() || process.env["ZVEC_GREP_MCP_URL"]?.trim() || DEFAULT_ZVEC_MCP_URL
  return value.replace(/\/+$/, "")
}

export interface ZvecMcpClientOptions {
  url: string
  fetch?: FetchLike
  requestTimeoutMs?: number
}

function toolResultText(result: ZvecMcpToolResult | undefined): string {
  const content = result?.content
  if (!Array.isArray(content)) return ""
  return content
    .map((part) => {
      if (!part || typeof part !== "object") return ""
      const text = (part as { text?: unknown }).text
      return typeof text === "string" ? text : ""
    })
    .filter(Boolean)
    .join("\n")
}

export async function runZvecMcpIndex(
  client: ZvecMcpToolCaller,
  directory: string,
  embedding: string,
  rebuild = false,
): Promise<void> {
  const result = await client.callTool("zvec_grep_index", {
    root: path.resolve(directory),
    embedding,
    rebuild,
    wait: true,
  })
  const structured = result.structuredContent
  if (!structured || typeof structured !== "object") {
    throw new Error("zvec-grep index returned no structured completion state")
  }
  const state = (structured as { state?: unknown }).state
  if (state !== "succeeded") {
    const error = (structured as { error?: { message?: unknown } }).error?.message
    throw new Error(typeof error === "string" ? error : `zvec-grep index ended in state ${String(state)}`)
  }
}

async function readJsonRpcMessage(response: Response): Promise<JsonRpcMessage> {
  if (!response.ok) {
    const body = await response.text().catch(() => "")
    throw new Error(`zvec-grep MCP HTTP ${response.status}: ${body.slice(0, 500)}`)
  }

  const contentType = response.headers.get("content-type") || ""
  if (contentType.includes("application/json")) {
    return await response.json() as JsonRpcMessage
  }

  const reader = response.body?.getReader()
  if (!reader) throw new Error("zvec-grep MCP returned no response body")

  const decoder = new TextDecoder()
  let buffer = ""
  try {
    while (true) {
      const next = await reader.read()
      if (next.done) break
      buffer += decoder.decode(next.value, { stream: true })
      const events = buffer.split(/\r?\n\r?\n/)
      buffer = events.pop() || ""
      for (const event of events) {
        const data = event
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice("data:".length).trimStart())
          .join("\n")
        if (!data || data === "[DONE]") continue
        return JSON.parse(data) as JsonRpcMessage
      }
    }
  } finally {
    await reader.cancel().catch(() => {})
  }
  throw new Error("zvec-grep MCP closed without a JSON-RPC response")
}

/** Minimal Streamable HTTP MCP client for the local zvec management endpoint. */
export class ZvecMcpClient implements ZvecMcpToolCaller {
  private readonly url: string
  private readonly fetcher: FetchLike
  private readonly requestTimeoutMs: number
  private nextID = 1
  private sessionID: string | undefined
  private initialization: Promise<void> | undefined

  constructor(options: ZvecMcpClientOptions) {
    this.url = options.url.trim().replace(/\/+$/, "")
    if (!this.url) throw new Error("zvec-grep MCP URL is empty")
    this.fetcher = options.fetch || fetch
    this.requestTimeoutMs = options.requestTimeoutMs || DEFAULT_REQUEST_TIMEOUT_MS
  }

  private async request(message: Record<string, unknown>, expectResponse = true): Promise<JsonRpcMessage | undefined> {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), this.requestTimeoutMs)
    try {
      const headers: Record<string, string> = {
        Accept: "application/json, text/event-stream",
        "Content-Type": "application/json",
      }
      if (this.sessionID) headers["Mcp-Session-Id"] = this.sessionID
      const response = await this.fetcher(this.url, {
        method: "POST",
        headers,
        body: JSON.stringify(message),
        signal: controller.signal,
      })
      const sessionID = response.headers.get("mcp-session-id")
      if (sessionID) this.sessionID = sessionID
      if (!expectResponse) {
        if (!response.ok) {
          const body = await response.text().catch(() => "")
          throw new Error(`zvec-grep MCP HTTP ${response.status}: ${body.slice(0, 500)}`)
        }
        await response.body?.cancel().catch(() => {})
        return undefined
      }
      return await readJsonRpcMessage(response)
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new Error(`zvec-grep MCP request timed out after ${this.requestTimeoutMs}ms`)
      }
      throw error
    } finally {
      clearTimeout(timeout)
    }
  }

  private async initialize(): Promise<void> {
    if (this.initialization) return this.initialization
    this.initialization = (async () => {
      const message = await this.request({
        jsonrpc: "2.0",
        id: this.nextID++,
        method: "initialize",
        params: {
          protocolVersion: MCP_PROTOCOL_VERSION,
          capabilities: {},
          clientInfo: { name: "engram-opencode-plugin", version: "0.1.0" },
        },
      })
      if (message?.error) {
        throw new Error(`zvec-grep MCP initialize failed: ${message.error.message || "unknown error"}`)
      }
      await this.request({
        jsonrpc: "2.0",
        method: "notifications/initialized",
        params: {},
      }, false)
    })().catch((error) => {
      this.initialization = undefined
      throw error
    })
    return this.initialization
  }

  async callTool(name: string, arguments_: Record<string, unknown>): Promise<ZvecMcpToolResult> {
    await this.initialize()
    const message = await this.request({
      jsonrpc: "2.0",
      id: this.nextID++,
      method: "tools/call",
      params: { name, arguments: arguments_ },
    })
    if (message?.error) {
      throw new Error(`zvec-grep MCP ${name} failed: ${message.error.message || "unknown error"}`)
    }
    const result = message?.result
    if (!result) throw new Error(`zvec-grep MCP ${name} returned no result`)
    if (result.isError) {
      throw new Error(`zvec-grep MCP ${name} failed${toolResultText(result) ? `: ${toolResultText(result)}` : ""}`)
    }
    return result
  }

  async close(): Promise<void> {
    if (!this.sessionID) return
    const sessionID = this.sessionID
    this.sessionID = undefined
    this.initialization = undefined
    try {
      await this.fetcher(this.url, {
        method: "DELETE",
        headers: { "Mcp-Session-Id": sessionID },
      })
    } catch {
      // Session cleanup must never turn plugin shutdown into a failure.
    }
  }
}
