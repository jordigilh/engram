// Engram plugin for OpenCode (https://opencode.ai).
//
// Gives OpenCode the same recall/retain/code-search/LSP capabilities Cursor
// already gets from Engram's shared daemon fleet (hindsight-docs,
// hindsight-issues, cocoindex-code, serena via serena_multiplex), through a
// single plugin with a minimal, optionally-zero config surface -- users
// never write MCP server blocks themselves. See
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
// `project` auto-detects from the directory name and current git branch
// (mirroring cocoindex_search's existing release-line convention); override
// only if the auto-detected name is wrong for your layout.
import type { Plugin } from "@opencode-ai/plugin"
import { buildMcpConfig, deriveIdentity, type EngramPluginOptions, type McpBackendUrls } from "./identity"

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
  const options = (rawOptions || {}) as EngramPluginOptions & McpBackendUrls
  const directoryBasename = (ctx.directory || "").split("/").filter(Boolean).pop() || "unknown-project"
  const branch = await detectBranch(ctx.directory, ctx.$)

  const identity = deriveIdentity({ directoryBasename, branch, options })
  const mcp = buildMcpConfig(identity, options)

  console.error(
    `[engram-plugin] project=${identity.project} family=${identity.family} branch=${identity.branchSuffix} directory=${ctx.directory}`,
  )

  return {
    config: async (config) => {
      config.mcp = config.mcp || {}
      Object.assign(config.mcp, mcp)
    },
  }
}

export default EngramPlugin
