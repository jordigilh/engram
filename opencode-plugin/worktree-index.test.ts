import { describe, expect, test } from "bun:test"
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import * as path from "node:path"
import {
  createWorktreeIndexer,
  allowedRootsForFamily,
  getZvecGitRevision,
  hasZvecManifest,
  isZvecWorkspaceReadTool,
  readZvecProvenance,
  shouldGateSessions,
  shouldAutoIndexWorktrees,
  type ZvecGitRevision,
  type ZvecIndexProvenance,
  writeZvecProvenance,
} from "./worktree-index"
import { runZvecMcpIndex } from "./zvec-mcp"

describe("worktree indexing policy", () => {
  test("requires explicit project configuration and supports global opt-in", () => {
    expect(shouldAutoIndexWorktrees("service-api", { projects: ["service-api"] })).toBe(true)
    expect(shouldAutoIndexWorktrees("service-api", { projects: ["service-api"], enabled: false })).toBe(false)
    expect(shouldAutoIndexWorktrees("service-ui")).toBe(false)
    expect(shouldAutoIndexWorktrees("service-ui", { enabled: true })).toBe(true)
    expect(shouldAutoIndexWorktrees("service-api-v1.5", { projects: ["service-api"] })).toBe(false)
    expect(shouldAutoIndexWorktrees("engram")).toBe(false)
    expect(shouldAutoIndexWorktrees("engram", { enabled: true })).toBe(true)
    expect(shouldGateSessions("service-api", { projects: ["service-api"] })).toBe(false)
    expect(shouldGateSessions("service-ui")).toBe(false)
    expect(shouldGateSessions("service-ui", { gateSessions: true })).toBe(true)
    expect(shouldGateSessions("service-api", { projects: ["service-api"], gateSessions: false })).toBe(false)
  })

  test("recognizes zvec workspace reads without matching the explicit index tool", () => {
    expect(isZvecWorkspaceReadTool("engram_zvec_grep_search")).toBe(true)
    expect(isZvecWorkspaceReadTool("mcp:engram:zvec_grep_callgraph_cluster")).toBe(true)
    expect(isZvecWorkspaceReadTool("zvec_grep_index")).toBe(false)
    expect(isZvecWorkspaceReadTool("grep")).toBe(false)
  })

  test("inherits exact zvec roots from the resolved repository family", () => {
    expect(allowedRootsForFamily({
      allowedRoots: ["/global", "/shared"],
      familyRoots: { kubernaut: ["/shared", "/kubernaut"], praxis: ["/praxis"] },
    }, "kubernaut")).toEqual(["/global", "/shared", "/kubernaut"])
    expect(allowedRootsForFamily({ familyRoots: { kubernaut: ["/kubernaut"] } }, "dcm")).toEqual([])
    expect(allowedRootsForFamily({ familyRoots: { constructor: ["/constructor"] } }, "constructor"))
      .toEqual(["/constructor"])
  })

  test("reads and writes manifest and Git provenance safely", async () => {
    const fixture = await mkdtemp(path.join(tmpdir(), "engram-zvec-provenance-"))
    const revision: ZvecGitRevision = { branch: "main", commit: "abc", dirty: true }
    try {
      expect(await hasZvecManifest(fixture)).toBe(false)
      await mkdir(path.join(fixture, ".zvec-grep"))
      await writeFile(path.join(fixture, ".zvec-grep", "manifest.json"), "{}\n")
      expect(await hasZvecManifest(fixture)).toBe(true)
      expect(await readZvecProvenance(fixture)).toBeUndefined()

      await writeZvecProvenance(fixture, revision)
      expect(await readZvecProvenance(fixture)).toEqual({
        version: 2,
        root: path.resolve(fixture),
        ...revision,
      })

      await writeFile(path.join(fixture, ".zvec-grep", "engram-git-provenance.json"), "not-json")
      expect(await readZvecProvenance(fixture)).toBeUndefined()
      await writeFile(
        path.join(fixture, ".zvec-grep", "engram-git-provenance.json"),
        JSON.stringify({ version: 2, root: "/other", ...revision }),
      )
      expect(await readZvecProvenance(fixture)).toBeUndefined()
    } finally {
      await rm(fixture, { recursive: true, force: true })
    }
  })

  test("gets Git provenance including dirty state", async () => {
    const revision = await getZvecGitRevision(process.cwd())
    expect(revision?.commit).toMatch(/^[0-9a-f]+$/)
    expect(typeof revision?.dirty).toBe("boolean")
  })

  test("submits root-scoped indexing through the container MCP tool", async () => {
    const calls: Array<{ name: string; arguments: Record<string, unknown> }> = []
    await runZvecMcpIndex({
      callTool: async (name, arguments_) => {
        calls.push({ name, arguments: arguments_ })
        return { structuredContent: { state: "succeeded" } }
      },
    }, "/worktrees/fix-123", "local/test", true)
    expect(calls).toEqual([{
      name: "zvec_grep_index",
      arguments: {
        root: "/worktrees/fix-123",
        embedding: "local/test",
        rebuild: true,
        wait: true,
      },
    }])
  })
})

describe("createWorktreeIndexer", () => {
  test("indexes unindexed siblings after matching worktree updates, but not the canonical checkout", async () => {
    const indexed = new Set<string>(["/worktrees/already-indexed"])
    const calls: string[] = []
    const logs: string[] = []
    const revision: ZvecGitRevision = { branch: "main", commit: "same-commit", dirty: false }
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      listWorktrees: async () => [
        { directory: "/repos/service-api" },
        { directory: "/worktrees/fix-123" },
        { directory: "/worktrees/already-indexed" },
      ],
      hasManifest: async (directory) => indexed.has(directory),
      getRevision: async () => revision,
      readProvenance: async (directory) => directory === "/worktrees/already-indexed"
        ? { version: 2, root: directory, ...revision }
        : undefined,
      runIndex: async (directory) => {
        calls.push(directory)
        indexed.add(directory)
      },
      writeProvenance: async () => {},
      log: (message) => logs.push(message),
    })

    await indexer.handleEvent({ type: "worktree.updated", data: { projectID: "project-service-api" } })
    await indexer.waitForIdle()

    expect(calls).toEqual(["/worktrees/fix-123"])
    expect(logs).toContain("finished zg index for worktree /worktrees/fix-123")
    indexer.dispose()
  })

  test("ensureIndexed waits for and verifies the current checkout index", async () => {
    let indexed = false
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      allowedRoots: ["/worktrees/current"],
      listWorktrees: async () => [],
      hasManifest: async () => indexed,
      getRevision: async () => ({ branch: "main", commit: "current", dirty: false }),
      readProvenance: async () => ({
        version: 2,
        root: "/worktrees/current",
        branch: "main",
        commit: "current",
        dirty: false,
      }),
      writeProvenance: async () => {},
      runIndex: async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 5))
        indexed = true
      },
      log: () => {},
    })

    await indexer.ensureIndexed("/worktrees/current")
    expect(indexed).toBe(true)
    indexer.dispose()
  })

  test("ensureIndexed propagates an indexing failure to the session gate", async () => {
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      allowedRoots: ["/worktrees/current"],
      listWorktrees: async () => [],
      hasManifest: async () => false,
      runIndex: async () => {
        throw new Error("Jieba dictionary unavailable")
      },
      log: () => {},
    })

    await expect(indexer.ensureIndexed("/worktrees/current")).rejects.toThrow("Jieba dictionary unavailable")
    indexer.dispose()
  })

  test("does not rebuild a manifest for a non-Git aggregate root", async () => {
    let runCount = 0
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      aggregateRoots: ["/worktree-pool"],
      listWorktrees: async () => [],
      hasManifest: async () => true,
      getRevision: async () => undefined,
      isGitWorktree: async () => false,
      runIndex: async () => {
        runCount += 1
      },
      log: () => {},
    })

    await indexer.ensureIndexed("/worktree-pool")
    await indexer.ensureIndexed("/worktree-pool")

    expect(runCount).toBe(0)
    indexer.dispose()
  })

  test("rejects a workspace-read root outside the explicit allowlist", async () => {
    let runCount = 0
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      allowedRoots: ["/worktrees/allowed"],
      listWorktrees: async () => [],
      hasManifest: async () => true,
      runIndex: async () => {
        runCount += 1
      },
      log: () => {},
    })

    await expect(indexer.ensureIndexed("/worktrees/not-allowed")).rejects.toThrow("not authorized")
    expect(runCount).toBe(0)
    indexer.dispose()
  })

  test("rebuilds when dirty state changes even if branch and commit stay the same", async () => {
    const revision: ZvecGitRevision = { branch: "main", commit: "same-commit", dirty: true }
    const previous: ZvecIndexProvenance = {
      version: 2,
      root: "/worktrees/dirty",
      branch: "main",
      commit: "same-commit",
      dirty: false,
    }
    const calls: Array<{ directory: string; rebuild: boolean }> = []
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      listWorktrees: async () => [{ directory: "/worktrees/dirty" }],
      hasManifest: async () => true,
      getRevision: async () => revision,
      readProvenance: async () => previous,
      writeProvenance: async () => {},
      runIndex: async (directory, rebuild) => calls.push({ directory, rebuild }),
      log: () => {},
    })

    await indexer.refresh()
    await indexer.waitForIdle()

    expect(calls).toEqual([{ directory: "/worktrees/dirty", rebuild: true }])
    indexer.dispose()
  })

  test("fails readiness when provenance cannot be persisted", async () => {
    let indexed = false
    const revision: ZvecGitRevision = { branch: "main", commit: "commit", dirty: false }
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      allowedRoots: ["/worktrees/provenance-failure"],
      listWorktrees: async () => [],
      hasManifest: async () => indexed,
      getRevision: async () => revision,
      runIndex: async () => {
        indexed = true
      },
      writeProvenance: async () => {
        throw new Error("sidecar is read-only")
      },
      log: () => {},
    })

    await expect(indexer.ensureIndexed("/worktrees/provenance-failure")).rejects.toThrow("read-only")
    indexer.dispose()
  })

  test("ignores updates from other projects", async () => {
    let listCalls = 0
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      listWorktrees: async () => {
        listCalls += 1
        return []
      },
      runIndex: async () => {},
    })

    await indexer.handleEvent({ type: "worktree.updated", data: { projectID: "project-other" } })

    expect(listCalls).toBe(0)
    indexer.dispose()
  })

  test("de-duplicates concurrent updates while one index build is running", async () => {
    const started: string[] = []
    let finishIndex: (() => void) | undefined
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      listWorktrees: async () => [{ directory: "/worktrees/fix-456" }],
      hasManifest: async () => false,
      runIndex: async (directory) => {
        started.push(directory)
        await new Promise<void>((resolve) => {
          finishIndex = resolve
        })
      },
      log: () => {},
    })

    await Promise.all([
      indexer.handleEvent({ type: "worktree.updated", data: { projectID: "project-service-api" } }),
      indexer.handleEvent({ type: "worktree.updated", data: { projectID: "project-service-api" } }),
    ])
    await Promise.resolve()
    expect(started).toEqual(["/worktrees/fix-456"])

    finishIndex?.()
    await indexer.waitForIdle()
    indexer.dispose()
  })

  test("a later worktree update retries an index that previously failed", async () => {
    const indexed = new Set<string>()
    const calls: string[] = []
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      listWorktrees: async () => [{ directory: "/worktrees/retry-me" }],
      hasManifest: async (directory) => indexed.has(directory),
      runIndex: async (directory) => {
        calls.push(directory)
        if (calls.length === 1) throw new Error("temporary zg failure")
        indexed.add(directory)
      },
      log: () => {},
    })

    const event = { type: "worktree.updated", data: { projectID: "project-service-api" } }
    await indexer.handleEvent(event)
    await indexer.waitForIdle()
    await indexer.handleEvent(event)
    await indexer.waitForIdle()

    expect(calls).toEqual(["/worktrees/retry-me", "/worktrees/retry-me"])
    indexer.dispose()
  })

  test("rebuilds an existing manifest when its Engram Git provenance is stale", async () => {
    const revision: ZvecGitRevision = { branch: "fix/new-route", commit: "new-commit", dirty: false }
    const previous: ZvecIndexProvenance = {
      version: 2,
      root: "/worktrees/stale-branch",
      branch: "main",
      commit: "old-commit",
      dirty: false,
    }
    const calls: Array<{ directory: string; rebuild: boolean }> = []
    let saved: ZvecIndexProvenance | undefined
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      listWorktrees: async () => [{ directory: "/worktrees/stale-branch" }],
      hasManifest: async () => true,
      getRevision: async () => revision,
      readProvenance: async () => previous,
      writeProvenance: async (directory, value) => {
        saved = { version: 2, root: directory, ...value }
      },
      runIndex: async (directory, rebuild) => calls.push({ directory, rebuild }),
      log: () => {},
    })

    await indexer.refresh()
    await indexer.waitForIdle()

    expect(calls).toEqual([{ directory: "/worktrees/stale-branch", rebuild: true }])
    expect(saved).toEqual({ version: 2, root: "/worktrees/stale-branch", ...revision })
    indexer.dispose()
  })

  test("does not rebuild again when the recorded Git provenance matches", async () => {
    const revision: ZvecGitRevision = { branch: "main", commit: "same-commit", dirty: false }
    const provenance: ZvecIndexProvenance = {
      version: 2,
      root: "/worktrees/current",
      ...revision,
    }
    const calls: Array<{ directory: string; rebuild: boolean }> = []
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      listWorktrees: async () => [{ directory: "/worktrees/current" }],
      hasManifest: async () => true,
      getRevision: async () => revision,
      readProvenance: async () => provenance,
      runIndex: async (directory, rebuild) => calls.push({ directory, rebuild }),
      log: () => {},
    })

    await indexer.refresh()
    await indexer.handleEvent({ type: "worktree.updated", data: { projectID: "project-service-api" } })
    await indexer.waitForIdle()

    expect(calls).toEqual([])
    indexer.dispose()
  })

  test("serializes a branch refresh and preserves a follow-up when Git changes during a build", async () => {
    const revisions: ZvecGitRevision[] = [
      { branch: "fix/new-route", commit: "new-commit", dirty: false },
      { branch: "fix/new-route", commit: "new-commit", dirty: false },
      { branch: "fix/new-route", commit: "newer-commit", dirty: false },
      { branch: "fix/new-route", commit: "newer-commit", dirty: false },
      { branch: "fix/new-route", commit: "newer-commit", dirty: false },
    ]
    const previous: ZvecIndexProvenance = {
      version: 2,
      root: "/worktrees/branch-race",
      branch: "main",
      commit: "old-commit",
      dirty: false,
    }
    const calls: Array<{ directory: string; rebuild: boolean }> = []
    const saved: ZvecIndexProvenance[] = []
    let getRevisionCall = 0
    let finishFirst: (() => void) | undefined
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      listWorktrees: async () => [{ directory: "/worktrees/branch-race" }],
      hasManifest: async () => true,
      getRevision: async () => revisions[Math.min(getRevisionCall++, revisions.length - 1)],
      readProvenance: async () => previous,
      writeProvenance: async (directory, revision) => saved.push({ version: 2, root: directory, ...revision }),
      runIndex: async (directory, rebuild) => {
        calls.push({ directory, rebuild })
        if (calls.length === 1) {
          await new Promise<void>((resolve) => {
            finishFirst = resolve
          })
        }
      },
      log: () => {},
    })

    const firstRefresh = indexer.refresh()
    while (calls.length === 0) await new Promise<void>((resolve) => setTimeout(resolve, 0))
    finishFirst?.()
    await firstRefresh
    await indexer.waitForIdle()

    expect(calls).toEqual([
      { directory: "/worktrees/branch-race", rebuild: true },
      { directory: "/worktrees/branch-race", rebuild: true },
    ])
    expect(saved).toEqual([{ version: 2, root: "/worktrees/branch-race", ...revisions[2] }])
    indexer.dispose()
  })
})
