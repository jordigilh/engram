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
import type { WorktreeIndexPolicy } from "./worktree-index"

export interface RepositoryIdentityOptions {
  project?: string
  family?: string
  /** Optional exact gateway routes keyed by raw branch or release suffix. */
  branchRoutes?: Record<string, string>
}

export interface EngramRepositoryMappings {
  /** Exact checkout directory names, including non-Git workspace roots. */
  directories?: Record<string, RepositoryIdentityOptions>
  /** Canonical Git remote URLs. SSH and .git suffixes are normalized. */
  remotes?: Record<string, RepositoryIdentityOptions>
}

export interface EngramPluginOptions extends RepositoryIdentityOptions {
  gatewayUrl?: string
  repositories?: EngramRepositoryMappings
  /** Deployment-local worktree indexing policy; projects must be explicitly listed. */
  worktreeIndex?: WorktreeIndexPolicy
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

export function normalizeRepositoryRemote(remote: string): string {
  let normalized = remote.trim().replace(/\/+$/, "")
  if (normalized.startsWith("git@")) {
    normalized = `https://${normalized.slice(4).replace(":", "/")}`
  } else if (normalized.startsWith("ssh://git@")) {
    normalized = `https://${normalized.slice("ssh://git@".length)}`
  }
  return normalized.replace(/\.git$/, "")
}

export function resolveRepositoryOptions(
  directoryBasename: string,
  remotes: string[],
  options: EngramPluginOptions,
): RepositoryIdentityOptions {
  const directoryOptions = options.repositories?.directories?.[directoryBasename]
  const remoteEntries = Object.entries(options.repositories?.remotes || {})
  const remoteOptions = remotes
    .map(normalizeRepositoryRemote)
    .map((remote) => remoteEntries.find(([configured]) => normalizeRepositoryRemote(configured) === remote)?.[1])
    .find(Boolean)
  const mapped = directoryOptions || remoteOptions || {}

  return {
    ...mapped,
    ...(options.project ? { project: options.project } : {}),
    ...(options.family ? { family: options.family } : {}),
    ...(options.branchRoutes ? { branchRoutes: options.branchRoutes } : {}),
  }
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

/** Native V2 (OpenCode 2.x) remote server shape. V2 enables OAuth by default,
 *  so the LAN gateway entry must opt out explicitly; `disabled: false` is the
 *  explicit enabled state for the server. */
export interface McpServerEntryV2 {
  type: "remote"
  url: string
  oauth: false
  disabled: false
  /**
   * Keep Engram on OpenCode's native MCP tool list rather than Code Mode.
   *
   * Code Mode is normally useful, but delegated workers have hit an
   * intermittent namespace-registration failure where a qualified path such
   * as `engram.docs_sync_retain` is qualified a second time and dispatched as
   * `engram.engram.docs_sync_retain`. Native MCP mode exposes the stable flat
   * `<server>_<tool>` names instead and is the safer bootstrap path.
   */
  codemode: false
}

/** Native V2 server entry for `ctx.mcp.transform(editor => editor.set(...))`.
 *  The gateway is a plain-LAN endpoint with no OAuth issuer, so `oauth: false`
 *  keeps V2 from attempting OAuth discovery against it. A missing gateway URL
 *  means deployment configuration is absent, so no generated fallback entry is
 *  returned. */
export function buildMcpServerConfigV2(
  identity: Pick<ResolvedIdentity, "project">,
  options: { gatewayUrl: string },
): McpServerEntryV2
export function buildMcpServerConfigV2(
  identity: Pick<ResolvedIdentity, "project">,
  options: Pick<EngramPluginOptions, "gatewayUrl"> = {},
): McpServerEntryV2 | undefined {
  const gatewayUrl = options.gatewayUrl?.trim().replace(/\/$/, "")
  if (!gatewayUrl) return undefined
  return {
    type: "remote",
    url: `${gatewayUrl}/mcp/${identity.project}`,
    oauth: false,
    disabled: false,
    codemode: false,
  }
}

/** Adds the generated route only when no explicit `engram` entry exists;
 *  never overwrites the configured route. */
export function mergeMcpEditor(
  editor: { get(name: string): unknown; set(name: string, config: McpServerEntryV2): void },
  generated: McpServerEntryV2,
): void {
  if (!editor.get("engram")) editor.set("engram", generated)
}
