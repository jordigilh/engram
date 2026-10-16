import { execFile } from "node:child_process"
import { access, mkdir, readFile, realpath, rename, unlink, writeFile } from "node:fs/promises"
import * as path from "node:path"
import {
  ZvecMcpClient,
  resolveZvecMcpUrl,
  runZvecMcpIndex,
  type FetchLike,
} from "./zvec-mcp"

export const DEFAULT_ZVEC_EMBEDDING = "local/potion-code-16m-v2"
const ZVEC_PROVENANCE_FILE = "engram-git-provenance.json"
const ZVEC_PROVENANCE_VERSION = 2
const MAX_READINESS_ATTEMPTS = 3

interface WorktreeEntry {
  directory: string
}

interface WorktreeUpdatedEvent {
  type?: unknown
  data?: { projectID?: unknown }
}

export interface ZvecGitRevision {
  branch: string | null
  commit: string
  /** Whether tracked or untracked checkout content differed at inspection time. */
  dirty: boolean
}

export interface ZvecIndexProvenance extends ZvecGitRevision {
  version: typeof ZVEC_PROVENANCE_VERSION
  root: string
}

export interface WorktreeIndexerOptions {
  projectID: string
  canonicalDirectory: string
  listWorktrees: () => Promise<readonly WorktreeEntry[]>
  mcpUrl?: string
  mcpFetch?: FetchLike
  embedding?: string
  /** Additional exact roots authorized for zvec indexing (for example aggregates). */
  allowedRoots?: readonly string[]
  /** Authorized roots whose freshness is managed by an external aggregate index. */
  aggregateRoots?: readonly string[]
  runIndex?: (directory: string, rebuild: boolean) => Promise<void>
  hasManifest?: (directory: string) => Promise<boolean>
  getRevision?: (directory: string) => Promise<ZvecGitRevision | undefined>
  isGitWorktree?: (directory: string) => Promise<boolean>
  readProvenance?: (directory: string) => Promise<ZvecIndexProvenance | undefined>
  writeProvenance?: (directory: string, revision: ZvecGitRevision) => Promise<void>
  log?: (message: string) => void
}

export interface WorktreeIndexPolicy {
  enabled?: boolean
  projects?: string[]
  /** Containerized zvec-grep MCP endpoint used for index lifecycle operations. */
  mcpUrl?: string
  embedding?: string
  /** Exact absolute roots permitted in zvec workspace-read tool arguments.
   *
   * This is deployment-wide rather than current-project-only: a family route
   * can use Engram's shared code index to compare a sibling repository. The
   * current checkout is always authorized separately; sibling roots must be
   * listed here (or in aggregateRoots for a managed non-Git aggregate).
   */
  allowedRoots?: string[]
  /** Exact roots inherited by every route with the named repository family. */
  familyRoots?: Record<string, string[]>
  /** Explicit non-Git roots with an aggregate zvec manifest. */
  aggregateRoots?: string[]
  /** Block the first prompt in a session until Engram and zvec-grep are ready. */
  gateSessions?: boolean
}

export function shouldAutoIndexWorktrees(project: string, policy: WorktreeIndexPolicy = {}): boolean {
  if (policy.enabled === false) return false
  if (policy.enabled === true) return true
  return policy.projects?.includes(project) === true
}

/**
 * Combine deployment-wide and family-scoped zvec authorization without
 * weakening the exact-root check performed by createWorktreeIndexer().
 * Family roots are inherited by every route in that family, so onboarding a
 * sibling repository does not require repeating the same list per project
 * route.
 */
export function allowedRootsForFamily(
  policy: WorktreeIndexPolicy = {},
  family?: string,
): string[] {
  const roots = [
    ...(policy.allowedRoots || []),
    ...(family ? policy.familyRoots?.[family] || [] : []),
  ]
  return [...new Set(roots)]
}

/** Session gating is opt-in because MCP and indexing are optional enhancements. */
export function shouldGateSessions(project: string, policy: WorktreeIndexPolicy = {}): boolean {
  if (policy.enabled === false || policy.gateSessions === false) return false
  return policy.gateSessions === true
}

const ZVEC_WORKSPACE_READ_TOOLS = [
  "zvec_grep_search",
  "zvec_grep_callgraph_blast_radius",
  "zvec_grep_callgraph_shortest_path",
  "zvec_grep_callgraph_cluster",
  "zvec_grep_callgraph_communities",
  "zvec_grep_codegraph_affected",
  "zvec_grep_codegraph_node",
  "zvec_grep_codegraph_neighbors",
  "zvec_grep_codegraph_relation_path",
  "zvec_grep_codegraph_explain",
  "zvec_search_code",
] as const

/** Match native MCP names as well as OpenCode's server-qualified names. */
export function isZvecWorkspaceReadTool(tool: string): boolean {
  const normalized = tool.trim().toLowerCase()
  return ZVEC_WORKSPACE_READ_TOOLS.some((name) =>
    normalized === name || normalized.endsWith(`_${name}`) || normalized.endsWith(`:${name}`),
  )
}

export function hasZvecManifest(directory: string): Promise<boolean> {
  return access(path.join(directory, ".zvec-grep", "manifest.json"))
    .then(() => true)
    .catch((error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") return false
      throw error
    })
}

function readGitValue(directory: string, args: string[]): Promise<string | undefined> {
  return new Promise((resolve) => {
    execFile(
      "git",
      args,
      { cwd: directory, timeout: 5000, encoding: "utf8" },
      (error, stdout) => {
        if (error) {
          resolve(undefined)
          return
        }
        const value = String(stdout || "").trim()
        resolve(value || undefined)
      },
    )
  })
}

/** Read the Git identity that was visible when a worktree was indexed. */
export async function getZvecGitRevision(directory: string): Promise<ZvecGitRevision | undefined> {
  const [branch, commit, dirty] = await Promise.all([
    readGitValue(directory, ["rev-parse", "--abbrev-ref", "HEAD"]),
    readGitValue(directory, ["rev-parse", "HEAD"]),
    readGitDirty(directory),
  ])
  if (!commit || dirty === undefined) return undefined
  return { branch: branch && branch !== "HEAD" ? branch : null, commit, dirty }
}

function readGitDirty(directory: string): Promise<boolean | undefined> {
  return new Promise((resolve) => {
    execFile(
      "git",
      ["status", "--porcelain=v1", "--untracked-files=all"],
      { cwd: directory, timeout: 5000, encoding: "utf8" },
      (error, stdout) => {
        if (error) {
          resolve(undefined)
          return
        }
        const dirtyPaths = String(stdout || "")
          .split(/\r?\n/)
          .map((line) => line.slice(3).trim().split(" -> ").pop() || "")
          .filter((value) => value && value !== ".zvec-grep" && !value.startsWith(".zvec-grep/"))
        resolve(dirtyPaths.length > 0)
      },
    )
  })
}

function provenancePath(directory: string): string {
  return path.join(directory, ".zvec-grep", ZVEC_PROVENANCE_FILE)
}

/** Read Engram-owned Git provenance without treating a missing sidecar as an error. */
export async function readZvecProvenance(directory: string): Promise<ZvecIndexProvenance | undefined> {
  let raw: string
  try {
    raw = await readFile(provenancePath(directory), "utf8")
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code
    if (code === "ENOENT") return undefined
    throw error
  }

  try {
    const value = JSON.parse(raw) as Partial<ZvecIndexProvenance>
    if (
      value.version !== ZVEC_PROVENANCE_VERSION ||
      value.root !== path.resolve(directory) ||
      typeof value.commit !== "string" ||
      (value.branch !== null && typeof value.branch !== "string") ||
      typeof value.dirty !== "boolean"
    ) {
      return undefined
    }
    return value as ZvecIndexProvenance
  } catch {
    // A truncated or hand-edited sidecar must never be trusted as freshness
    // evidence. The next setup will take the safe rebuild path.
    return undefined
  }
}

/** Persist provenance atomically after a successful index operation. */
export async function writeZvecProvenance(
  directory: string,
  revision: ZvecGitRevision,
): Promise<void> {
  const target = provenancePath(directory)
  const temporary = `${target}.${process.pid}.${Date.now()}.tmp`
  const value: ZvecIndexProvenance = {
    version: ZVEC_PROVENANCE_VERSION,
    root: path.resolve(directory),
    branch: revision.branch,
    commit: revision.commit,
    dirty: revision.dirty,
  }
  try {
    await mkdir(path.dirname(target), { recursive: true })
    await writeFile(temporary, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600 })
    await rename(temporary, target)
  } catch (error) {
    await unlink(temporary).catch(() => {})
    throw error
  }
}

function revisionsMatch(left: ZvecGitRevision, right: ZvecIndexProvenance): boolean {
  return left.branch === right.branch && left.commit === right.commit && left.dirty === right.dirty
}

async function realpathOrResolved(directory: string): Promise<string> {
  try {
    return await realpath(directory)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return path.resolve(directory)
    throw error
  }
}

/**
 * Watches OpenCode's worktree inventory and serially maintains each configured
 * sibling worktree's local zvec index. The canonical checkout is deliberately
 * excluded. Engram stores the Git branch/commit beside the ZG manifest because
 * ZG's workspace manifest has no Git provenance; a missing or changed
 * provenance record takes the explicit --rebuild path rather than trusting an
 * index that may belong to another branch.
 */
export function createWorktreeIndexer(options: WorktreeIndexerOptions) {
  const canonicalDirectory = path.resolve(options.canonicalDirectory)
  const authorizedRoots = new Map<string, string>()
  const aggregateRoots = new Set<string>()
  const rememberRoot = (directory: string, aggregate = false): void => {
    if (!path.isAbsolute(directory)) return
    const normalized = path.resolve(directory)
    authorizedRoots.set(normalized, normalized)
    if (aggregate) aggregateRoots.add(normalized)
  }
  rememberRoot(canonicalDirectory)
  for (const directory of options.allowedRoots || []) rememberRoot(directory)
  for (const directory of options.aggregateRoots || []) rememberRoot(directory, true)
  const rebuildAfterRun = new Set<string>()
  const log = options.log || ((message: string) => console.error(`[engram-plugin] ${message}`))
  const hasManifest = options.hasManifest || hasZvecManifest
  const getRevision = options.getRevision || getZvecGitRevision
  const isGitWorktree = options.isGitWorktree || (async (directory: string) =>
    (await readGitValue(directory, ["rev-parse", "--is-inside-work-tree"])) === "true")
  const readProvenance = options.readProvenance || readZvecProvenance
  const writeProvenance = options.writeProvenance || writeZvecProvenance
  const mcpClient = options.runIndex
    ? undefined
    : new ZvecMcpClient({
        url: resolveZvecMcpUrl(options.mcpUrl),
        fetch: options.mcpFetch,
      })
  const runIndex = options.runIndex
    || ((directory: string, rebuild: boolean) => runZvecMcpIndex(
      mcpClient!,
      directory,
      options.embedding || DEFAULT_ZVEC_EMBEDDING,
      rebuild,
    ))

  let disposed = false
  let refreshQueue: Promise<void> = Promise.resolve()
  let indexQueue: Promise<void> = Promise.resolve()
  const pendingPromises = new Map<string, Promise<void>>()

  async function refreshAuthorizedRoots(): Promise<void> {
    let entries: readonly WorktreeEntry[]
    try {
      entries = await options.listWorktrees()
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      log(`could not refresh authorized worktree roots: ${message}`)
      return
    }
    for (const entry of entries) {
      if (entry && typeof entry.directory === "string" && path.isAbsolute(entry.directory)) {
        rememberRoot(entry.directory)
      }
    }
    for (const [normalized] of authorizedRoots) {
      authorizedRoots.set(normalized, await realpathOrResolved(normalized))
    }
  }

  async function authorizeRoot(directory: string): Promise<{ normalized: string; aggregate: boolean }> {
    await refreshAuthorizedRoots()
    const requested = path.resolve(directory)
    const requestedReal = await realpathOrResolved(requested)
    for (const [configured, real] of authorizedRoots) {
      if (configured !== requested && real !== requested && real !== requestedReal) continue
      return {
        normalized: requestedReal,
        aggregate: aggregateRoots.has(configured),
      }
    }
    throw new Error(
      `zvec root is not authorized: ${requested}; configure worktreeIndex.allowedRoots or aggregateRoots`,
    )
  }

  type IndexDecision = {
    shouldIndex: boolean
    rebuild: boolean
    revision?: ZvecGitRevision
    reason: "missing" | "missing-provenance" | "git-revision-changed" | "git-revision-unavailable" | "non-git-manifest"
  }

  async function decideIndex(directory: string, aggregate = false): Promise<IndexDecision> {
    if (!(await hasManifest(directory))) {
      return { shouldIndex: true, rebuild: false, reason: "missing" }
    }

    let revision: ZvecGitRevision | undefined
    try {
      revision = await getRevision(directory)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      log(`could not inspect Git revision for ${directory}: ${message}`)
    }
    if (!revision) {
      let gitWorktree = true
      try {
        gitWorktree = await isGitWorktree(directory)
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        log(`could not determine whether ${directory} is a Git worktree: ${message}`)
      }
      if (!gitWorktree) {
        if (!aggregate) {
          throw new Error(
            `non-Git zvec root ${directory} is not authorized; declare it in worktreeIndex.aggregateRoots`,
          )
        }
        // Explicit aggregate roots can legitimately have no Git provenance.
        // Their own zvec manifest is the readiness/freshness contract.
        return { shouldIndex: false, rebuild: false, reason: "non-git-manifest" }
      }
      log(`could not determine Git revision for ${directory}; rebuilding from the current worktree`)
      return { shouldIndex: true, rebuild: true, reason: "git-revision-unavailable" }
    }

    let provenance: ZvecIndexProvenance | undefined
    try {
      provenance = await readProvenance(directory)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      log(`could not inspect Engram ZG provenance for ${directory}: ${message}`)
    }
    if (!provenance) {
      return { shouldIndex: true, rebuild: true, revision, reason: "missing-provenance" }
    }
    if (!revisionsMatch(revision, provenance)) {
      return { shouldIndex: true, rebuild: true, revision, reason: "git-revision-changed" }
    }
    return { shouldIndex: false, rebuild: false, revision, reason: "missing" }
  }

  function enqueueIndex(directory: string, rebuild = false, aggregate = false): Promise<void> {
    const normalized = path.resolve(directory)
    if (disposed) return Promise.resolve()
    const existing = pendingPromises.get(normalized)
    if (existing) {
      if (rebuild) rebuildAfterRun.add(normalized)
      return existing
    }

    const task = indexQueue.then(async () => {
      if (disposed) return
      const decision = await decideIndex(normalized, aggregate)
      if (!decision.shouldIndex) return
      const revisionBefore = decision.revision
      const operation = decision.rebuild ? "rebuild" : "index"
      log(`starting zg ${operation} for worktree ${normalized} (${decision.reason})`)
      await runIndex(normalized, decision.rebuild)
      if (!(await hasManifest(normalized))) {
        throw new Error("zg --index completed without creating .zvec-grep/manifest.json")
      }

      // A checkout can change while a long local embedding run is in
      // progress. Never record the old revision as fresh in that case;
      // schedule a second explicit rebuild after this serialized operation.
      const revisionAfter = await getRevision(normalized).catch(() => undefined)
      if (revisionBefore && revisionAfter && !revisionsMatch(revisionBefore, {
        version: ZVEC_PROVENANCE_VERSION,
        root: normalized,
        ...revisionAfter,
      })) {
        rebuildAfterRun.add(normalized)
        log(`Git revision changed during zg ${operation} for ${normalized}; scheduling a fresh rebuild`)
      } else if (revisionAfter) {
        try {
          await writeProvenance(normalized, revisionAfter)
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          log(`could not persist Engram ZG provenance for ${normalized}: ${message}`)
          throw new Error(`could not persist Engram ZG provenance for ${normalized}: ${message}`, { cause: error })
        }
      } else if (!aggregate) {
        throw new Error(`could not prove Git freshness for ${normalized}; refusing to mark zvec ready`)
      }
      log(`finished zg index for worktree ${normalized}`)
    })

    // Keep the serialized queue usable after a failed background operation,
    // while exposing the original rejection to a session readiness gate.
    indexQueue = task.catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error)
      log(`zg index failed for worktree ${normalized}: ${message}`)
    }).then(() => undefined)

    const result = task.finally(() => {
      pendingPromises.delete(normalized)
      if (!disposed && rebuildAfterRun.delete(normalized)) void enqueueIndex(normalized, true)
    })
    pendingPromises.set(normalized, result)
    // Background refreshes intentionally do not surface failures to the
    // event stream. The prompt gate awaits this same promise and does see the
    // rejection.
    void result.catch(() => {})
    return result
  }

  /** Ensure one checkout has a complete index before prompt admission. */
  async function ensureIndexed(directory: string): Promise<void> {
    const authorization = await authorizeRoot(directory)
    const normalized = authorization.normalized
    if (disposed) throw new Error("worktree indexer is disposed")

    for (let attempt = 1; attempt <= MAX_READINESS_ATTEMPTS; attempt += 1) {
      const pendingIndex = pendingPromises.get(normalized)
      if (pendingIndex) await pendingIndex

      const decision = await decideIndex(normalized, authorization.aggregate)
      if (!decision.shouldIndex) {
        // An event can enqueue a rebuild between decideIndex and this return.
        // Observe it before declaring the session ready.
        const concurrent = pendingPromises.get(normalized)
        if (concurrent) {
          await concurrent
          continue
        }
        return
      }

      await enqueueIndex(normalized, decision.rebuild, authorization.aggregate)

      // A non-Git checkout cannot carry Engram provenance. The manifest is
      // still a valid readiness signal, but do not loop forever trying to
      // rebuild an index whose revision can never be recorded.
      const revision = await getRevision(normalized).catch(() => undefined)
      if (!revision) {
        if (authorization.aggregate && (await hasManifest(normalized))) return
        throw new Error(`could not prove Git freshness for ${normalized}; refusing to mark zvec ready`)
      }
    }
    throw new Error(
      `worktree ${normalized} changed during ${MAX_READINESS_ATTEMPTS} zvec index attempts; retry after the checkout settles`,
    )
  }

  async function scan(): Promise<void> {
    if (disposed) return
    let entries: readonly WorktreeEntry[]
    try {
      entries = await options.listWorktrees()
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      log(`could not list worktrees for zvec indexing: ${message}`)
      return
    }

    for (const entry of entries) {
      if (!entry || typeof entry.directory !== "string" || !path.isAbsolute(entry.directory)) continue
      const directory = path.resolve(entry.directory)
      if (directory === canonicalDirectory) continue
      let decision: IndexDecision
      try {
        const authorization = await authorizeRoot(directory)
        decision = await decideIndex(authorization.normalized, authorization.aggregate)
        if (!decision.shouldIndex) continue
        enqueueIndex(authorization.normalized, decision.rebuild, authorization.aggregate)
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        log(`could not inspect ZG setup for ${directory}: ${message}`)
        continue
      }
    }
  }

  function refresh(): Promise<void> {
    if (disposed) return Promise.resolve()
    refreshQueue = refreshQueue.then(scan, scan)
    return refreshQueue
  }

  function handleEvent(event: unknown): Promise<void> {
    const update = event as WorktreeUpdatedEvent | null
    if (update?.type !== "worktree.updated" || update.data?.projectID !== options.projectID) {
      return Promise.resolve()
    }
    return refresh()
  }

  return {
    refresh,
    handleEvent,
    ensureIndexed,
    waitForIdle: async () => {
      while (true) {
        const currentRefresh = refreshQueue
        const currentIndex = indexQueue
        await currentRefresh
        await currentIndex
        if (currentRefresh === refreshQueue && currentIndex === indexQueue) return
      }
    },
    dispose: () => {
      disposed = true
      void mcpClient?.close()
    },
  }
}
