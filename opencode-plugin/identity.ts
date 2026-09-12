// Pure derivation logic for the Engram OpenCode plugin's minimal config
// surface. Kept dependency-free (no OpenCode/Bun APIs) so it's directly
// unit-testable; index.ts wires this to real `ctx` (directory, git branch).
//
// Two independent identities, both optional in user-facing config:
//   - `project`: exact Engram gateway route. Set this for a registered release
//     alias or a repository whose route differs from its directory name.
//   - `family` : shared bank identity metadata. The gateway owns the actual
//     docs/issues backend mapping.
//
// See docs/findings/2026-08.md (2026-08-13, thirteenth-sixteenth follow-ups)
// and https://github.com/jordigilh/engram/issues/22 for the design spikes
// this implements.

export interface EngramPluginOptions {
  project?: string
  family?: string
  gatewayUrl?: string
  /** Optional exact gateway routes keyed by raw branch or release suffix. */
  branchRoutes?: Record<string, string>
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

  const baseName = input.directoryBasename.replace(RELEASE_DIR_SUFFIX, "")
  // Gateway routes are explicit registry keys. A configured branch route is
  // safe to select automatically; without one, keep the historical exact
  // directory-name behavior rather than inventing an unregistered route.
  const branchRoute = options.branchRoutes && [
    input.branch,
    branchSuffix,
    branchSuffix === "main" ? "main" : `release/${branchSuffix}`,
  ].filter((key): key is string => Boolean(key)).map((key) => options.branchRoutes?.[key]).find(Boolean)
  const project = options.project || branchRoute || input.directoryBasename
  const family = options.family || baseName

  return { project, family, branchSuffix }
}

export interface McpServerEntry {
  type: "remote"
  url: string
  enabled: true
}

export type McpConfig = Record<"engram", McpServerEntry>

const DEFAULT_GATEWAY_URL = "http://127.0.0.1:8896"

export function buildMcpConfig(
  identity: Pick<ResolvedIdentity, "project">,
  options: Pick<EngramPluginOptions, "gatewayUrl"> = {},
): McpConfig {
  const gatewayUrl = (options.gatewayUrl || DEFAULT_GATEWAY_URL).replace(/\/$/, "")
  return {
    engram: { type: "remote", url: `${gatewayUrl}/mcp/${identity.project}`, enabled: true },
  }
}
