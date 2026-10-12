import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import { access, mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import * as path from "node:path"

export const DEFAULT_ZVEC_EMBEDDING = "local/potion-code-16m-v2"
const ZVEC_PROVENANCE_FILE = "engram-git-provenance.json"
const ZVEC_PROVENANCE_VERSION = 1

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
}

export interface ZvecIndexProvenance extends ZvecGitRevision {
  version: typeof ZVEC_PROVENANCE_VERSION
  root: string
}

export interface WorktreeIndexerOptions {
  projectID: string
  canonicalDirectory: string
  listWorktrees: () => Promise<readonly WorktreeEntry[]>
  binary?: string
  embedding?: string
  /** Optional environment setting required by local zvec builds (for example Jieba dictionaries). */
  jiebaDictionary?: string
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
  binary?: string
  embedding?: string
  /** Optional environment setting required by local zvec builds (for example Jieba dictionaries). */
  jiebaDictionary?: string
  /** Block the first prompt in a session until Engram and zvec-grep are ready. */
  gateSessions?: boolean
}

export function shouldAutoIndexWorktrees(project: string, policy: WorktreeIndexPolicy = {}): boolean {
  if (policy.enabled === false) return false
  if (policy.enabled === true) return true
  return policy.projects?.includes(project) === true
}

/** Session gating is opt-in because MCP and indexing are optional enhancements. */
export function shouldGateSessions(project: string, policy: WorktreeIndexPolicy = {}): boolean {
  if (policy.enabled === false || policy.gateSessions === false) return false
  return policy.gateSessions === true
}

export function resolveZvecBinary(configured?: string): string {
  const explicit = configured?.trim()
  if (explicit) return explicit

  const homeBinary = path.join(homedir(), "bin", "zg")
  return existsSync(homeBinary) ? homeBinary : "zg"
}

export function buildZvecIndexArgs(directory: string, embedding: string, rebuild = false): string[] {
  return [
    "--index",
    directory,
    ...(rebuild ? ["--rebuild"] : []),
    "--mode",
    "auto",
    "--embedding",
    embedding,
  ]
}

/** Build the child environment without changing the plugin process environment. */
export function buildZvecIndexEnvironment(jiebaDictionary?: string): NodeJS.ProcessEnv {
  const configured = jiebaDictionary?.trim()
  return {
    ...process.env,
    ...(configured ? { ZVEC_JIEBA_DICT_DIR: configured } : {}),
  }
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
  const [branch, commit] = await Promise.all([
    readGitValue(directory, ["rev-parse", "--abbrev-ref", "HEAD"]),
    readGitValue(directory, ["rev-parse", "HEAD"]),
  ])
  if (!commit) return undefined
  return { branch: branch && branch !== "HEAD" ? branch : null, commit }
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
      (value.branch !== null && typeof value.branch !== "string")
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
  return left.branch === right.branch && left.commit === right.commit
}

export function runZvecIndex(
  binary: string,
  directory: string,
  embedding: string,
  rebuild = false,
  jiebaDictionary?: string,
): Promise<void> {
  const home = process.env["HOME"] || homedir()
  const zvecHome = process.env["ZVEC_GREP_HOME"] || path.join(home, ".engram", "zvec-grep")

  return new Promise((resolve, reject) => {
    execFile(
      binary,
      buildZvecIndexArgs(directory, embedding, rebuild),
      {
        cwd: directory,
        env: {
          ...buildZvecIndexEnvironment(jiebaDictionary),
          ZVEC_GREP_HOME: zvecHome,
        },
        timeout: 60 * 60 * 1000,
        maxBuffer: 16 * 1024 * 1024,
        encoding: "utf8",
      },
      (error, stdout, stderr) => {
        if (!error) {
          resolve()
          return
        }
        const details = [error.message, String(stderr || "").trim(), String(stdout || "").trim()]
          .filter(Boolean)
          .join("\n")
        reject(new Error(details))
      },
    )
  })
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
  const rebuildAfterRun = new Set<string>()
  const log = options.log || ((message: string) => console.error(`[engram-plugin] ${message}`))
  const hasManifest = options.hasManifest || hasZvecManifest
  const getRevision = options.getRevision || getZvecGitRevision
  const isGitWorktree = options.isGitWorktree || (async (directory: string) =>
    (await readGitValue(directory, ["rev-parse", "--is-inside-work-tree"])) === "true")
  const readProvenance = options.readProvenance || readZvecProvenance
  const writeProvenance = options.writeProvenance || writeZvecProvenance
  const runIndex = options.runIndex
    || ((directory: string, rebuild: boolean) => runZvecIndex(
      resolveZvecBinary(options.binary),
      directory,
      options.embedding || DEFAULT_ZVEC_EMBEDDING,
      rebuild,
      options.jiebaDictionary,
    ))

  let disposed = false
  let refreshQueue: Promise<void> = Promise.resolve()
  let indexQueue: Promise<void> = Promise.resolve()
  const pendingPromises = new Map<string, Promise<void>>()

  type IndexDecision = {
    shouldIndex: boolean
    rebuild: boolean
    revision?: ZvecGitRevision
    reason: "missing" | "missing-provenance" | "git-revision-changed" | "git-revision-unavailable" | "non-git-manifest"
  }

  async function decideIndex(directory: string): Promise<IndexDecision> {
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
        // OpenChamber can expose an aggregate directory containing several
        // ephemeral checkouts. It is a valid zvec root even though Git cannot
        // provide branch provenance for the aggregate itself. Once its
        // manifest exists, keep serving it instead of rebuilding on every
        // root-scoped search.
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

  function enqueueIndex(directory: string, rebuild = false): Promise<void> {
    const normalized = path.resolve(directory)
    if (disposed) return Promise.resolve()
    const existing = pendingPromises.get(normalized)
    if (existing) {
      if (rebuild) rebuildAfterRun.add(normalized)
      return existing
    }

    const task = indexQueue.then(async () => {
      if (disposed) return
      const decision = await decideIndex(normalized)
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
      if (
        revisionBefore &&
        revisionAfter &&
        (revisionBefore.branch !== revisionAfter.branch || revisionBefore.commit !== revisionAfter.commit)
      ) {
        rebuildAfterRun.add(normalized)
        log(`Git revision changed during zg ${operation} for ${normalized}; scheduling a fresh rebuild`)
      } else if (revisionAfter) {
        try {
          await writeProvenance(normalized, revisionAfter)
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          log(`could not persist Engram ZG provenance for ${normalized}: ${message}`)
        }
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
    const normalized = path.resolve(directory)
    if (disposed) throw new Error("worktree indexer is disposed")

    while (true) {
      const pendingIndex = pendingPromises.get(normalized)
      if (pendingIndex) await pendingIndex

      const decision = await decideIndex(normalized)
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

      await enqueueIndex(normalized, decision.rebuild)

      // A non-Git checkout cannot carry Engram provenance. The manifest is
      // still a valid readiness signal, but do not loop forever trying to
      // rebuild an index whose revision can never be recorded.
      const revision = await getRevision(normalized).catch(() => undefined)
      if (!revision && (await hasManifest(normalized))) return
    }
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
        decision = await decideIndex(directory)
        if (!decision.shouldIndex) continue
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        log(`could not inspect ZG setup for ${directory}: ${message}`)
        continue
      }
      enqueueIndex(directory, decision.rebuild)
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
    },
  }
}
