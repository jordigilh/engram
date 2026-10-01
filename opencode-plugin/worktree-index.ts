import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import { access, mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import * as path from "node:path"

export const DEFAULT_KUBERNAUT_ZVEC_EMBEDDING = "local/potion-code-16m-v2"
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

export interface KubernautWorktreeIndexerOptions {
  projectID: string
  canonicalDirectory: string
  listWorktrees: () => Promise<readonly WorktreeEntry[]>
  binary?: string
  embedding?: string
  runIndex?: (directory: string, rebuild: boolean) => Promise<void>
  hasManifest?: (directory: string) => Promise<boolean>
  getRevision?: (directory: string) => Promise<ZvecGitRevision | undefined>
  readProvenance?: (directory: string) => Promise<ZvecIndexProvenance | undefined>
  writeProvenance?: (directory: string, revision: ZvecGitRevision) => Promise<void>
  log?: (message: string) => void
}

export function shouldAutoIndexKubernautWorktrees(project: string, enabled?: boolean): boolean {
  return enabled === true || (project === "kubernaut" && enabled !== false)
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
): Promise<void> {
  const home = process.env["HOME"] || homedir()
  const zvecHome = process.env["ZVEC_GREP_HOME"] || path.join(home, ".engram", "zvec-grep")

  return new Promise((resolve, reject) => {
    execFile(
      binary,
      buildZvecIndexArgs(directory, embedding, rebuild),
      {
        cwd: directory,
        env: { ...process.env, ZVEC_GREP_HOME: zvecHome },
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
 * Watches OpenCode's worktree inventory and serially maintains each sibling
 * Kubernaut worktree's local zvec index. The canonical checkout is deliberately
 * excluded. Engram stores the Git branch/commit beside the ZG manifest because
 * ZG's workspace manifest has no Git provenance; a missing or changed
 * provenance record takes the explicit --rebuild path rather than trusting an
 * index that may belong to another branch.
 */
export function createKubernautWorktreeIndexer(options: KubernautWorktreeIndexerOptions) {
  const canonicalDirectory = path.resolve(options.canonicalDirectory)
  const pending = new Set<string>()
  const rebuildAfterRun = new Set<string>()
  const log = options.log || ((message: string) => console.error(`[engram-plugin] ${message}`))
  const hasManifest = options.hasManifest || hasZvecManifest
  const getRevision = options.getRevision || getZvecGitRevision
  const readProvenance = options.readProvenance || readZvecProvenance
  const writeProvenance = options.writeProvenance || writeZvecProvenance
  const runIndex = options.runIndex
    || ((directory: string, rebuild: boolean) => runZvecIndex(
      resolveZvecBinary(options.binary),
      directory,
      options.embedding || DEFAULT_KUBERNAUT_ZVEC_EMBEDDING,
      rebuild,
    ))

  let disposed = false
  let refreshQueue: Promise<void> = Promise.resolve()
  let indexQueue: Promise<void> = Promise.resolve()

  type IndexDecision = {
    shouldIndex: boolean
    rebuild: boolean
    revision?: ZvecGitRevision
    reason: "missing" | "missing-provenance" | "git-revision-changed" | "git-revision-unavailable"
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

  function enqueueIndex(directory: string, rebuild = false): void {
    if (disposed) return
    if (pending.has(directory)) {
      if (rebuild) rebuildAfterRun.add(directory)
      return
    }
    pending.add(directory)
    indexQueue = indexQueue
      .then(async () => {
        if (disposed) return
        const decision = await decideIndex(directory)
        if (!decision.shouldIndex) return
        const revisionBefore = decision.revision
        const operation = decision.rebuild ? "rebuild" : "index"
        log(`starting zg ${operation} for Kubernaut worktree ${directory} (${decision.reason})`)
        await runIndex(directory, decision.rebuild)
        if (!(await hasManifest(directory))) {
          throw new Error("zg --index completed without creating .zvec-grep/manifest.json")
        }

        // A checkout can change while a long local embedding run is in
        // progress. Never record the old revision as fresh in that case;
        // schedule a second explicit rebuild after this serialized operation.
        const revisionAfter = await getRevision(directory).catch(() => undefined)
        if (
          revisionBefore &&
          revisionAfter &&
          (revisionBefore.branch !== revisionAfter.branch || revisionBefore.commit !== revisionAfter.commit)
        ) {
          rebuildAfterRun.add(directory)
          log(`Git revision changed during zg ${operation} for ${directory}; scheduling a fresh rebuild`)
        } else if (revisionAfter) {
          try {
            await writeProvenance(directory, revisionAfter)
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error)
            log(`could not persist Engram ZG provenance for ${directory}: ${message}`)
          }
        }
        log(`finished zg index for Kubernaut worktree ${directory}`)
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error)
        log(`zg index failed for Kubernaut worktree ${directory}: ${message}`)
      })
      .finally(() => {
        pending.delete(directory)
        if (!disposed && rebuildAfterRun.delete(directory)) enqueueIndex(directory)
      })
  }

  async function scan(): Promise<void> {
    if (disposed) return
    let entries: readonly WorktreeEntry[]
    try {
      entries = await options.listWorktrees()
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      log(`could not list Kubernaut worktrees for zvec indexing: ${message}`)
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
