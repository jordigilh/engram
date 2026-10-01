// Engram plugin for OpenCode 2 (https://opencode.ai).
//
// Gives OpenCode/OpenChamber recall/retain/code-search capabilities through
// one Engram gateway MCP entry, so users never register backend servers
// individually. See
// https://github.com/jordigilh/engram/issues/22 for the design writeup and
// docs/findings/2026-08.md (2026-08-13, 13th-16th follow-ups) for the spikes
// this implements.
//
// OpenCode V2 registers MCP configuration and behavior through Plugin.define
// and its MCP, session, tool, and event APIs. Configure the plugin under
// `plugins`; V1 plugin implementations are not loaded by OpenCode V2.
//
// `project` defaults to the directory name. Set it explicitly for a registered
// gateway alias such as a release-line route whose name differs from the
// checkout directory.
import { Plugin } from "@opencode/plugin"
import { execFile } from "node:child_process"
import {
  buildMcpServerConfigV2,
  deriveIdentity,
  mergeMcpEditor,
  resolveRepositoryOptions,
  type EngramPluginOptions,
  type McpServerEntryV2,
  type ResolvedIdentity,
} from "./identity"
import { buildCompactionContext } from "./compaction"
import { buildSystemRecall } from "./system-recall"
import { buildCompactedLog } from "./compacted-observer"
import { shouldNudgeMcp, buildMcpNudgeMessage } from "./mcp-nudge"
import { detectCodeSearch } from "./code-search"
import {
  normalizeSearchKey,
  NudgeDedupe,
  ProbeCache,
  probeGateway,
  buildProbedNudge,
} from "./probe"
import {
  buildNudgeEvent,
  buildEngramToolUseEvent,
  isEngramTool,
  type MetricEvent,
} from "./metrics"
import {
  createWorktreeIndexer,
  DEFAULT_ZVEC_EMBEDDING,
  shouldAutoIndexWorktrees,
} from "./worktree-index"
import { resolveEngramOptions } from "./user-config"

function runGit(args: string[], directory: string, timeoutMs = 5000): Promise<string> {
  return new Promise((resolve) => {
    execFile("git", args, { cwd: directory, timeout: timeoutMs }, (error, stdout) => {
      if (error) resolve("")
      else resolve(String(stdout || ""))
    })
  })
}

async function detectBranch(directory: string): Promise<string | undefined> {
  try {
    const out = await runGit(["rev-parse", "--abbrev-ref", "HEAD"], directory)
    const branch = out.trim()
    if (!branch) return undefined
    return branch === "HEAD" ? undefined : branch // detached HEAD: treat as unknown, not a branch name
  } catch {
    return undefined
  }
}

async function detectRemotes(directory: string): Promise<string[]> {
  try {
    const out = await runGit(["remote", "-v"], directory)
    return out
      .split(/\r?\n/)
      .filter((line: string) => /\(fetch\)$/.test(line.trim()))
      .map((line: string) => line.trim().split(/\s+/)[1])
      .filter(Boolean)
  } catch {
    return []
  }
}

interface EngramState {
  identity: ResolvedIdentity
  gatewayUrl?: string
  mcpV2?: McpServerEntryV2
}

async function resolveEngramState(directory: string, options: EngramPluginOptions): Promise<EngramState> {
  const directoryBasename = (directory || "").split("/").filter(Boolean).pop() || "unknown-project"
  const branch = await detectBranch(directory)
  const remotes = await detectRemotes(directory)
  const identityOptions = resolveRepositoryOptions(directoryBasename, remotes, options)

  const identity = deriveIdentity({ directoryBasename, branch, options: identityOptions })
  const mcpV2 = buildMcpServerConfigV2(identity, options)
  return {
    identity,
    gatewayUrl: mcpV2?.url,
    mcpV2,
  }
}

function metricsPath(): string {
  return (
    process.env["ENGRAM_METRICS_PATH"] ||
    `${process.env["HOME"] || "~"}/.engram/logs/opencode-nudges.jsonl`
  )
}

function makeLogMetric(): (event: MetricEvent) => void {
  const path = metricsPath()
  return (event: MetricEvent): void => {
    import("node:fs/promises")
      .then(async (fs) => {
        const line = `${JSON.stringify(event)}\n`
        try {
          await fs.appendFile(path, line, "utf-8")
        } catch {
          const dir = path.split("/").slice(0, -1).join("/") || "."
          try {
            await fs.mkdir(dir, { recursive: true })
            await fs.appendFile(path, line, "utf-8")
          } catch {
            /* metrics must never break the session */
          }
        }
      })
      .catch(() => {})
  }
}

function logProjectLine(identity: ResolvedIdentity, directory: string): void {
  console.error(
    `[engram-plugin] project=${identity.project} family=${identity.family} branch=${identity.branchSuffix} directory=${directory}`,
  )
}

// Append a probe nudge to a V2 Tool.Result without changing its shape:
// string content stays a string, array content gains a text part, and
// missing content becomes the nudge text. Never duplicates.
export function appendV2Nudge<T extends { content?: unknown }>(result: T, text: string): T
export function appendV2Nudge(result: undefined, text: string): undefined
export function appendV2Nudge<T extends { content?: unknown }>(result: T | undefined, text: string): T | undefined {
  if (!result) return result
  const content = result.content
  if (typeof content === "string") {
    if (content.includes("[engram-plugin]")) return result
    return { ...result, content: `${content}\n\n${text}` } as T
  }
  if (Array.isArray(content)) {
    if (
      content.some(
        (part) =>
          part &&
          typeof part === "object" &&
          "text" in part &&
          typeof part.text === "string" &&
          part.text.includes("[engram-plugin]"),
      )
    ) {
      return result
    }
    return { ...result, content: [...content, { type: "text", text }] } as T
  }
  if (content == null) {
    return { ...result, content: text } as T
  }
  return result
}

function readV2SessionID(event: unknown): string {
  const e = event as {
    sessionID?: unknown
    properties?: Record<string, unknown>
    data?: Record<string, unknown>
  }
  const direct = typeof e.sessionID === "string" ? e.sessionID : ""
  if (direct) return direct
  for (const bag of [e.properties, e.data]) {
    const v = bag?.["sessionID"]
    if (typeof v === "string" && v) return v
  }
  return ""
}

// ---------------------------------------------------------------------------
// Register behavior through the V2 domain APIs.
// ---------------------------------------------------------------------------
type PluginContext = Parameters<Parameters<typeof Plugin.define>[0]["setup"]>[0]

async function setupEngramPlugin(ctx: PluginContext): Promise<(() => void) | void> {
  const directory = ctx.location.directory || ""
  const options = await resolveEngramOptions(ctx.options)
  const { identity, mcpV2, gatewayUrl: configuredGatewayUrl } = await resolveEngramState(directory, options)
  let gatewayUrl = configuredGatewayUrl
  const canonicalDirectory = ctx.location.project?.canonical || directory
  const worktreeProjectIdentity = canonicalDirectory === directory
    ? identity
    : (await resolveEngramState(canonicalDirectory, options)).identity
  const dedupe = new NudgeDedupe()
  const probeCache = new ProbeCache()
  const pendingProbe = new Set<string>()
  const logMetric = makeLogMetric()

  logProjectLine(identity, directory)

  // The direct config entry (mcp.servers.engram) is the route authority; the
  // generated entry is only a fallback and never overwrites an explicit one.
  await ctx.mcp.transform((editor) => {
    const explicit = editor.get("engram") as { url?: unknown } | undefined
    if (!gatewayUrl && typeof explicit?.url === "string" && explicit.url.trim()) {
      gatewayUrl = explicit.url
    }
    if (mcpV2) mergeMcpEditor(editor, mcpV2)
  })

  // Every agent-loop model request: re-apply methodology recall even if a
  // compaction summary dropped it.
  await ctx.session.hook("context", (event) => {
    event.system.push({ type: "text", text: buildSystemRecall(identity) })
  })

  // Compaction summaries: steer the summarizer so the methodology recall
  // survives `/compact` and auto-overflow.
  await ctx.session.hook("compaction", (event) => {
    event.system.push({ type: "text", text: buildCompactionContext(identity) })
  })

  // MCP-over-CLI nudge: warn-only, never blocks. Allowlist-based.
  await ctx.tool.hook("execute.before", (event) => {
    const input = event.input as { command?: unknown } | undefined
    const hit = shouldNudgeMcp(event.tool, input?.command)
    if (hit) console.error(`[engram-plugin] ${buildMcpNudgeMessage(hit)}`)
    if (isEngramTool(event.tool)) {
      logMetric(buildEngramToolUseEvent(event.sessionID, event.tool))
    }
  })

  // Probe-once code-search hint for the LLM: append-only on success.
  // Flow: detect → dedupe claim → cached probe or live probe (2.5s) →
  // append only when Engram actually has hits.
  await ctx.tool.hook("execute.after", async (event) => {
    if (event.status !== "completed") return
    const search = detectCodeSearch(event.tool, event.input)
    if (!search) return
    if (!gatewayUrl) return
    const key = normalizeSearchKey(search.cli, search.query || search.cli)
    const sessionID = event.sessionID
    const cached = probeCache.get(key)
    if (cached) {
      if (!cached.ok) return
      if (!dedupe.claim(sessionID, key)) return
      event.result = appendV2Nudge(event.result, buildProbedNudge(search.cli, search.query, cached.hits))
      logMetric(buildNudgeEvent(sessionID, search.cli, search.query, cached.hits))
      return
    }
    if (pendingProbe.has(key)) return
    pendingProbe.add(key)
    try {
      const query = search.query || search.cli
      const result = await probeGateway(gatewayUrl, query, fetch as never, 2500)
      probeCache.set(key, result)
      if (!result.ok) return
      if (!dedupe.claim(sessionID, key)) return
      event.result = appendV2Nudge(event.result, buildProbedNudge(search.cli, search.query, result.hits))
      logMetric(buildNudgeEvent(sessionID, search.cli, search.query, result.hits))
    } catch {
      return
    } finally {
      pendingProbe.delete(key)
    }
  })

  // OpenCode/OpenChamber worktree operations publish worktree.updated on the
  // canonical project. Indexing is opt-in per deployment-configured route;
  // every sibling worktree receives an independent local zvec index.
  const worktreePolicy = options.worktreeIndex || {}
  const worktreeIndexer = shouldAutoIndexWorktrees(
    worktreeProjectIdentity.project,
    worktreePolicy,
  )
    ? createWorktreeIndexer({
        projectID: ctx.location.project.id,
        canonicalDirectory,
        listWorktrees: () => ctx.worktree.list({ projectID: ctx.location.project.id }),
        binary: worktreePolicy.binary,
        embedding: worktreePolicy.embedding || DEFAULT_ZVEC_EMBEDDING,
      })
    : undefined

  // Also reconcile roots that were created just before the plugin started or
  // while OpenCode was reconnecting its event stream. This runs in the
  // background and never delays session startup.
  if (worktreeIndexer) void worktreeIndexer.refresh()

  // Post-compaction observer: low-risk, never blocks.
  const controller = new AbortController()
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        const type = (event as { type?: unknown }).type
        if (typeof type !== "string") continue
        if (type === "worktree.updated" && worktreeIndexer) {
          void worktreeIndexer.handleEvent(event).catch((error: unknown) => {
            const message = error instanceof Error ? error.message : String(error)
            console.error(`[engram-plugin] worktree indexer event failed: ${message}`)
          })
        }
        const line = buildCompactedLog(type, readV2SessionID(event))
        if (line) console.error(line)
      }
    } catch {
      /* subscription aborted on unload */
    }
  })()

  return () => {
    controller.abort()
    worktreeIndexer?.dispose()
  }
}

export default Plugin.define({
  id: "engram",
  setup: setupEngramPlugin,
})
