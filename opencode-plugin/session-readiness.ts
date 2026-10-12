const DEFAULT_MCP_READY_TIMEOUT_MS = 30_000
const DEFAULT_MCP_POLL_INTERVAL_MS = 250

export interface SessionReadinessOptions {
  listMcp: () => Promise<unknown>
  ensureIndex: () => Promise<void>
  timeoutMs?: number
  pollIntervalMs?: number
  sleep?: (milliseconds: number) => Promise<void>
  now?: () => number
}

type McpServerStatus = {
  name?: unknown
  status?: unknown
}

function statusName(value: unknown): string | undefined {
  if (typeof value === "string") return value
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  const nested = (value as { status?: unknown }).status
  return typeof nested === "string" ? nested : undefined
}

function serversFromMcpList(value: unknown): McpServerStatus[] {
  if (!value || typeof value !== "object") return []
  const data = (value as { data?: unknown }).data
  if (!Array.isArray(data)) return []
  return data.filter((server): server is McpServerStatus => Boolean(server) && typeof server === "object")
}

export function engramMcpStatus(value: unknown): string {
  const server = serversFromMcpList(value).find((candidate) => candidate.name === "engram")
  return statusName(server?.status) || "missing"
}

export function isEngramMcpConnected(value: unknown): boolean {
  return engramMcpStatus(value) === "connected"
}

function defaultSleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

/** Wait until the session's configured Engram MCP server is connected. */
export async function waitForEngramMcp(options: Pick<SessionReadinessOptions, "listMcp" | "timeoutMs" | "pollIntervalMs" | "sleep" | "now">): Promise<void> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_MCP_READY_TIMEOUT_MS
  const pollIntervalMs = options.pollIntervalMs ?? DEFAULT_MCP_POLL_INTERVAL_MS
  const sleep = options.sleep || defaultSleep
  const now = options.now || Date.now
  const deadline = now() + Math.max(0, timeoutMs)
  let lastStatus = "missing"

  while (true) {
    try {
      const result = await options.listMcp()
      lastStatus = engramMcpStatus(result)
      if (isEngramMcpConnected(result)) return
    } catch (error) {
      lastStatus = error instanceof Error ? error.message : String(error)
    }

    const remaining = deadline - now()
    if (remaining <= 0) break
    await sleep(Math.min(pollIntervalMs, remaining))
  }

  throw new Error(
    `[engram-plugin] Engram MCP did not become available before the session started (status: ${lastStatus}); ` +
      "check the configured gateway route and reload OpenCode",
  )
}

/** Make prompt admission wait for both Engram MCP and the current worktree index. */
export async function ensureEngramSessionReady(options: SessionReadinessOptions): Promise<void> {
  await waitForEngramMcp(options)
  try {
    await options.ensureIndex()
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    throw new Error(`[engram-plugin] zvec-grep is not ready for this session: ${message}`)
  }
}
