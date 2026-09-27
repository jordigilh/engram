import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import { access } from "node:fs/promises"
import { homedir } from "node:os"
import * as path from "node:path"

export const DEFAULT_KUBERNAUT_ZVEC_EMBEDDING = "local/potion-code-16m-v2"

interface WorktreeEntry {
  directory: string
}

interface WorktreeUpdatedEvent {
  type?: unknown
  data?: { projectID?: unknown }
}

export interface KubernautWorktreeIndexerOptions {
  projectID: string
  canonicalDirectory: string
  listWorktrees: () => Promise<readonly WorktreeEntry[]>
  binary?: string
  embedding?: string
  runIndex?: (directory: string) => Promise<void>
  hasManifest?: (directory: string) => Promise<boolean>
  log?: (message: string) => void
}

export function shouldAutoIndexKubernautWorktrees(project: string, enabled?: boolean): boolean {
  return project === "kubernaut" && enabled !== false
}

export function resolveZvecBinary(configured?: string): string {
  const explicit = configured?.trim()
  if (explicit) return explicit

  const homeBinary = path.join(homedir(), "bin", "zg")
  return existsSync(homeBinary) ? homeBinary : "zg"
}

export function buildZvecIndexArgs(directory: string, embedding: string): string[] {
  return ["--index", directory, "--mode", "auto", "--embedding", embedding]
}

export function hasZvecManifest(directory: string): Promise<boolean> {
  return access(path.join(directory, ".zvec-grep", "manifest.json"))
    .then(() => true)
    .catch((error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") return false
      throw error
    })
}

export function runZvecIndex(
  binary: string,
  directory: string,
  embedding: string,
): Promise<void> {
  const home = process.env["HOME"] || homedir()
  const zvecHome = process.env["ZVEC_GREP_HOME"] || path.join(home, ".engram", "zvec-grep")

  return new Promise((resolve, reject) => {
    execFile(
      binary,
      buildZvecIndexArgs(directory, embedding),
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
 * Watches OpenCode's worktree inventory and serially indexes any unindexed
 * Kubernaut worktree. The canonical checkout is deliberately excluded.
 * Inventory refreshes are cheap; actual index builds are de-duplicated and
 * serialized to avoid competing local embedding jobs.
 */
export function createKubernautWorktreeIndexer(options: KubernautWorktreeIndexerOptions) {
  const canonicalDirectory = path.resolve(options.canonicalDirectory)
  const pending = new Set<string>()
  const log = options.log || ((message: string) => console.error(`[engram-plugin] ${message}`))
  const hasManifest = options.hasManifest || hasZvecManifest
  const runIndex = options.runIndex
    || ((directory: string) => runZvecIndex(
      resolveZvecBinary(options.binary),
      directory,
      options.embedding || DEFAULT_KUBERNAUT_ZVEC_EMBEDDING,
    ))

  let disposed = false
  let refreshQueue: Promise<void> = Promise.resolve()
  let indexQueue: Promise<void> = Promise.resolve()

  function enqueueIndex(directory: string): void {
    if (disposed || pending.has(directory)) return
    pending.add(directory)
    indexQueue = indexQueue
      .then(async () => {
        if (await hasManifest(directory)) return
        log(`starting zg index for Kubernaut worktree ${directory}`)
        await runIndex(directory)
        if (!(await hasManifest(directory))) {
          throw new Error("zg --index completed without creating .zvec-grep/manifest.json")
        }
        log(`finished zg index for Kubernaut worktree ${directory}`)
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error)
        log(`zg index failed for Kubernaut worktree ${directory}: ${message}`)
      })
      .finally(() => {
        pending.delete(directory)
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
      if (directory === canonicalDirectory || pending.has(directory)) continue
      try {
        if (await hasManifest(directory) || pending.has(directory)) continue
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        log(`could not inspect zvec manifest for ${directory}: ${message}`)
        continue
      }
      enqueueIndex(directory)
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
      await refreshQueue
      await indexQueue
    },
    dispose: () => {
      disposed = true
    },
  }
}
