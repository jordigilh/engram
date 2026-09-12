// Engram plugin for OpenCode (https://opencode.ai).
//
// Gives OpenCode/OpenChamber recall/retain/code-search capabilities through
// one Engram gateway MCP entry, so users never register backend servers
// individually. See
// https://github.com/jordigilh/engram/issues/22 for the design writeup and
// docs/findings/2026-08.md (2026-08-13, 13th-16th follow-ups) for the spikes
// this implements.
//
// Usage, in a repo's opencode.json:
//   Single repo, zero config:
//     { "plugin": ["<path-or-package>/index.ts"] }
//   Org sharing one memory bank across sibling repos (set identically in
//   each repo's opencode.json):
//     { "plugin": [["<path-or-package>/index.ts", { "family": "kubernaut" }]] }
//
// `project` defaults to the directory name. Set it explicitly for a registered
// gateway alias such as a release-line route whose name differs from the
// checkout directory.
import type { Plugin } from "@opencode-ai/plugin"
import { buildMcpConfig, deriveIdentity, type EngramPluginOptions } from "./identity"
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

async function detectBranch(directory: string, $: any): Promise<string | undefined> {
  try {
    const out = await $`git rev-parse --abbrev-ref HEAD`.cwd(directory).quiet().text()
    const branch = out.trim()
    return branch === "HEAD" ? undefined : branch // detached HEAD: treat as unknown, not a branch name
  } catch {
    return undefined
  }
}

export const EngramPlugin: Plugin = async (ctx, rawOptions) => {
  const options = (rawOptions || {}) as EngramPluginOptions
  const directoryBasename = (ctx.directory || "").split("/").filter(Boolean).pop() || "unknown-project"
  const branch = await detectBranch(ctx.directory, ctx.$)

  const identity = deriveIdentity({ directoryBasename, branch, options })
  const mcp = buildMcpConfig(identity, options)
  const gatewayUrl = mcp.engram.url
  // Option A state: per-process probe cache + per-session dedupe so a
  // repeated grep can't re-trigger the gateway or spam the model.
  const dedupe = new NudgeDedupe()
  const probeCache = new ProbeCache()
  const pendingProbe = new Set<string>()

  // Steering metric: append-only jsonl, never throws. Read with
  // `bun opencode-plugin/metrics-report.ts` for nudge→MCP conversion.
  const metricsPath =
    process.env["ENGRAM_METRICS_PATH"] ||
    `${process.env["HOME"] || "~"}/.engram/logs/opencode-nudges.jsonl`
  const logMetric = (event: MetricEvent): void => {
    import("node:fs/promises")
      .then(async (fs) => {
        const line = `${JSON.stringify(event)}\n`
        try {
          await fs.appendFile(metricsPath, line, "utf-8")
        } catch {
          const dir = metricsPath.split("/").slice(0, -1).join("/") || "."
          try {
            await fs.mkdir(dir, { recursive: true })
            await fs.appendFile(metricsPath, line, "utf-8")
          } catch {
            /* metrics must never break the session */
          }
        }
      })
      .catch(() => {})
  }

  console.error(
    `[engram-plugin] project=${identity.project} family=${identity.family} branch=${identity.branchSuffix} directory=${ctx.directory}`,
  )

  return {
    config: async (config) => {
      config.mcp = config.mcp || {}
      Object.assign(config.mcp, mcp)
    },
    // Survive context compression. Fires before the LLM builds the
    // continuation summary on both `/compact` and auto-overflow.
    "experimental.session.compacting": async (_input, output) => {
      output.context.push(buildCompactionContext(identity))
    },
    // Every-prompt fallback: re-applies methodology recall even if a
    // compaction summary drops it. TUI-only `tui.prompt.append` is not used
    // so CLI/`serve`/web get the same guarantee.
    "experimental.chat.system.transform": async (_input, output) => {
      output.system.push(buildSystemRecall(identity))
    },
    // Post-compaction observer: low-risk, never blocks.
    event: async ({ event }) => {
      const props = (event as unknown as { properties?: Record<string, unknown> }).properties || {}
      const sessionID =
        (props["sessionID"] as string) ||
        ((event as unknown as { sessionID?: string }).sessionID ?? "")
      const line = buildCompactedLog(event.type, sessionID)
      if (line) console.error(line)
    },
    // MCP-over-CLI nudge: warn-only, never blocks. Allowlist-based.
    // Code-search is NOT logged here — it only nudges after a successful
    // gateway probe in `tool.execute.after`, so we never respond regardless.
    "tool.execute.before": async (input, output) => {
      const hit = shouldNudgeMcp(input.tool, output.args?.command)
      if (hit) console.error(`[engram-plugin] ${buildMcpNudgeMessage(hit)}`)
      if (isEngramTool(input.tool)) {
        logMetric(buildEngramToolUseEvent(input.sessionID || "", input.tool))
      }
    },
    // Probe-once code-search hint for the LLM: append-only, tool succeeds.
    // Flow: detect → dedupe claim → cached probe or live probe (2.5s) →
    // append only when Engram actually has hits.
    "tool.execute.after": async (input, output) => {
      const search = detectCodeSearch(input.tool, input.args)
      if (!search) return
      if (typeof output.output !== "string" || output.output.includes("[engram-plugin]")) return
      const key = normalizeSearchKey(search.cli, search.query || search.cli)
      const sessionID = input.sessionID || ""
      const cached = probeCache.get(key)
      if (cached) {
        if (!cached.ok) return
        if (!dedupe.claim(sessionID, key)) return
        output.output += `\n\n${buildProbedNudge(search.cli, search.query, cached.hits)}`
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
        output.output += `\n\n${buildProbedNudge(search.cli, search.query, result.hits)}`
        logMetric(buildNudgeEvent(sessionID, search.cli, search.query, result.hits))
      } catch {
        return
      } finally {
        pendingProbe.delete(key)
      }
    },
  }
}

export default EngramPlugin
