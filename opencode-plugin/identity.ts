// Pure derivation logic for the Engram OpenCode plugin's minimal config
// surface. Kept dependency-free (no OpenCode/Bun APIs) so it's directly
// unit-testable; index.ts wires this to real `ctx` (directory, git branch).
//
// Two independent identities, both optional in user-facing config:
//   - `project`: per-repo/per-branch. Drives serena's per-codebase isolation.
//   - `family` : shared bank identity. Drives hindsight-docs/issues naming.
//     Defaults to the repo's base name (never branch-suffixed) so a single
//     repo needs zero config, while an org with multiple repos sets `family`
//     once, identically, across sibling repos' opencode.json to share a bank.
//
// See docs/findings/2026-08.md (2026-08-13, thirteenth-sixteenth follow-ups)
// and https://github.com/jordigilh/engram/issues/22 for the design spikes
// this implements.

export interface EngramPluginOptions {
  project?: string
  family?: string
}

export interface DeriveIdentityInput {
  directoryBasename: string
  /** Current git branch name, or undefined if it couldn't be detected. */
  branch: string | undefined
  options?: EngramPluginOptions
}

export interface ResolvedIdentity {
  project: string
  family: string
  /** "main", or "vX.Y" when on/named for a release line. Informational. */
  branchSuffix: string
}

const RELEASE_DIR_SUFFIX = /-v(\d+\.\d+)$/
const RELEASE_BRANCH = /^release\/v(\d+\.\d+)$/

function detectBranchSuffix(directoryBasename: string, branch: string | undefined): string {
  const dirHint = directoryBasename.match(RELEASE_DIR_SUFFIX)
  if (dirHint) return `v${dirHint[1]}`

  if (branch) {
    const branchHint = branch.match(RELEASE_BRANCH)
    if (branchHint) return `v${branchHint[1]}`
  }

  return "main"
}

export function deriveIdentity(input: DeriveIdentityInput): ResolvedIdentity {
  const options = input.options || {}
  const branchSuffix = detectBranchSuffix(input.directoryBasename, input.branch)

  const baseName = (options.project || input.directoryBasename).replace(RELEASE_DIR_SUFFIX, "")
  const project = branchSuffix === "main" ? baseName : `${baseName}-${branchSuffix}`
  const family = options.family || baseName

  return { project, family, branchSuffix }
}

export interface McpBackendUrls {
  hindsightBaseUrl?: string
  cocoindexUrl?: string
  serenaMultiplexUrl?: string
}

export interface McpServerEntry {
  type: "remote"
  url: string
  enabled: true
}

export type McpConfig = Record<"hindsight-docs" | "hindsight-issues" | "cocoindex-code" | "serena", McpServerEntry>

const DEFAULT_HINDSIGHT_BASE_URL = "http://localhost:8888"
const DEFAULT_COCOINDEX_URL = "http://127.0.0.1:8891/mcp"
const DEFAULT_SERENA_MULTIPLEX_URL = "http://127.0.0.1:8893"

export function buildMcpConfig(identity: Pick<ResolvedIdentity, "project" | "family">, urls: McpBackendUrls = {}): McpConfig {
  const hindsightBaseUrl = urls.hindsightBaseUrl || DEFAULT_HINDSIGHT_BASE_URL
  const cocoindexUrl = urls.cocoindexUrl || DEFAULT_COCOINDEX_URL
  const serenaMultiplexUrl = urls.serenaMultiplexUrl || DEFAULT_SERENA_MULTIPLEX_URL

  return {
    "hindsight-docs": { type: "remote", url: `${hindsightBaseUrl}/mcp/${identity.family}-docs/`, enabled: true },
    "hindsight-issues": { type: "remote", url: `${hindsightBaseUrl}/mcp/${identity.family}-issues/`, enabled: true },
    "cocoindex-code": { type: "remote", url: cocoindexUrl, enabled: true },
    "serena": { type: "remote", url: `${serenaMultiplexUrl}/mcp/${identity.project}`, enabled: true },
  }
}
