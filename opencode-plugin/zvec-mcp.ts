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

const SESSION_LOSS_MARKERS = ["session not found", "missing session", "invalid session"]

export class ZvecMcpSessionLostError extends Error {
  readonly code = "MCP_SESSION_LOST"
  readonly status: number

  constructor(status: number, body: string) {
    super(`zvec-grep MCP session lost (HTTP ${status}): ${body.slice(0, 500)}`)
    this.name = "ZvecMcpSessionLostError"
    this.status = status
  }
}

function isSessionLossResponse(status: number, body: string): boolean {
  if (status !== 400 && status !== 404) return false
  const normalized = body.toLowerCase()
  return SESSION_LOSS_MARKERS.some((marker) => normalized.includes(marker))
}

export function zvecMcpErrorCode(error: unknown): string {
  if (error instanceof ZvecMcpSessionLostError) return error.code
  if (error && typeof error === "object") {
    const code = (error as { code?: unknown }).code
    if (
      typeof code === "string" &&
      (code === "MCP_SESSION_LOST" || code.startsWith("ZVEC_"))
    ) {
      return code
    }
  }
  return "ZVEC_READINESS_FAILED"
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
    if (isSessionLossResponse(response.status, body)) {
      throw new ZvecMcpSessionLostError(response.status, body)
    }
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
  private recovery: Promise<void> | undefined
  private sessionGeneration = 0
  private closed = false
  private readonly activeControllers = new Set<AbortController>()

  constructor(options: ZvecMcpClientOptions) {
    this.url = options.url.trim().replace(/\/+$/, "")
    if (!this.url) throw new Error("zvec-grep MCP URL is empty")
    this.fetcher = options.fetch || fetch
    this.requestTimeoutMs = options.requestTimeoutMs || DEFAULT_REQUEST_TIMEOUT_MS
  }

  private async request(message: Record<string, unknown>, expectResponse = true): Promise<JsonRpcMessage | undefined> {
    if (this.closed) throw new Error("zvec-grep MCP client is closed")

    const requestGeneration = this.sessionGeneration
    const controller = new AbortController()
    this.activeControllers.add(controller)
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
      // A delayed response from the previous session must not overwrite the
      // session established by a recovery that completed while this request
      // was in flight.
      if (sessionID && requestGeneration === this.sessionGeneration && !this.closed) {
        this.sessionID = sessionID
      }
      if (!expectResponse) {
        if (!response.ok) {
          const body = await response.text().catch(() => "")
          if (isSessionLossResponse(response.status, body)) {
            throw new ZvecMcpSessionLostError(response.status, body)
          }
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
      this.activeControllers.delete(controller)
    }
  }

  private async initialize(): Promise<void> {
    if (this.closed) throw new Error("zvec-grep MCP client is closed")
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
      this.sessionID = undefined
      this.initialization = undefined
      throw error
    })
    return this.initialization
  }

  private resetSession(): void {
    this.sessionGeneration += 1
    this.sessionID = undefined
    this.initialization = undefined
  }

  private async recoverSession(staleSessionID: string | undefined): Promise<void> {
    if (this.closed) throw new Error("zvec-grep MCP client is closed")

    // Await an active recovery before inspecting the session ID. The new ID
    // is assigned during initialize, before notifications/initialized has
    // completed, so a different ID alone is not proof that recovery is ready.
    if (this.recovery) {
      await this.recovery
      if (this.closed) throw new Error("zvec-grep MCP client is closed")
      return
    }

    // A concurrent caller may already have completed a recovery. Do not
    // discard the healthy replacement.
    if (staleSessionID && this.sessionID && this.sessionID !== staleSessionID) return

    const recovery = (async () => {
      if (this.closed) throw new Error("zvec-grep MCP client is closed")
      this.resetSession()
      await this.initialize()
      if (this.closed) throw new Error("zvec-grep MCP client is closed")
    })()
    this.recovery = recovery
    try {
      await recovery
    } finally {
      if (this.recovery === recovery) this.recovery = undefined
    }
  }

  async callTool(name: string, arguments_: Record<string, unknown>): Promise<ZvecMcpToolResult> {
    let staleSessionID = this.sessionID
    try {
      await this.initialize()
    } catch (error) {
      if (!(error instanceof ZvecMcpSessionLostError)) throw error
      await this.recoverSession(staleSessionID)
    }
    if (this.closed) throw new Error("zvec-grep MCP client is closed")
    staleSessionID = this.sessionID

    const call = async (): Promise<ZvecMcpToolResult> => {
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

    try {
      return await call()
    } catch (error) {
      if (!(error instanceof ZvecMcpSessionLostError)) throw error
      await this.recoverSession(staleSessionID)
      if (this.closed) throw new Error("zvec-grep MCP client is closed")
      return call()
    }
  }

  async close(): Promise<void> {
    const sessionID = this.sessionID
    this.closed = true
    this.resetSession()
    for (const controller of this.activeControllers) controller.abort()
    if (!sessionID) return
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
