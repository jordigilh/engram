import { describe, expect, test } from "bun:test"
import {
  buildZvecIndexArgs,
  createWorktreeIndexer,
  shouldAutoIndexWorktrees,
  type ZvecGitRevision,
  type ZvecIndexProvenance,
} from "./worktree-index"

describe("worktree indexing policy", () => {
  test("requires explicit project configuration and supports global opt-in", () => {
    expect(shouldAutoIndexWorktrees("service-api", { projects: ["service-api"] })).toBe(true)
    expect(shouldAutoIndexWorktrees("service-api", { projects: ["service-api"], enabled: false })).toBe(false)
    expect(shouldAutoIndexWorktrees("service-ui")).toBe(false)
    expect(shouldAutoIndexWorktrees("service-ui", { enabled: true })).toBe(true)
    expect(shouldAutoIndexWorktrees("service-api-v1.5", { projects: ["service-api"] })).toBe(false)
    expect(shouldAutoIndexWorktrees("engram")).toBe(false)
    expect(shouldAutoIndexWorktrees("engram", { enabled: true })).toBe(true)
  })

  test("uses auto mode and the configured local embedding for a new worktree", () => {
    expect(buildZvecIndexArgs("/worktrees/fix-123", "local/potion-code-16m-v2")).toEqual([
      "--index",
      "/worktrees/fix-123",
      "--mode",
      "auto",
      "--embedding",
      "local/potion-code-16m-v2",
    ])
  })

  test("adds --rebuild only when a branch-aware refresh is required", () => {
    expect(buildZvecIndexArgs("/worktrees/fix-123", "local/potion-code-16m-v2", true)).toEqual([
      "--index",
      "/worktrees/fix-123",
      "--rebuild",
      "--mode",
      "auto",
      "--embedding",
      "local/potion-code-16m-v2",
    ])
  })
})

describe("createWorktreeIndexer", () => {
  test("indexes unindexed siblings after matching worktree updates, but not the canonical checkout", async () => {
    const indexed = new Set<string>(["/worktrees/already-indexed"])
    const calls: string[] = []
    const logs: string[] = []
    const revision: ZvecGitRevision = { branch: "main", commit: "same-commit" }
    const indexer = createWorktreeIndexer({
      projectID: "project-service-api",
      canonicalDirectory: "/repos/service-api",
      listWorktrees: async () => [
        { directory: "/repos/service-api" },
        { directory: "/worktrees/fix-123" },
        { directory: "/worktrees/already-indexed" },
      ],
      hasManifest: async (directory) => indexed.has(directory),
      getRevision: async (directory) => directory === "/worktrees/already-indexed" ? revision : undefined,
      readProvenance: async (directory) => directory === "/worktrees/already-indexed"
        ? { version: 1, root: directory, ...revision }
        : undefined,
      runIndex: async (directory) => {
        calls.push(directory)
        indexed.add(directory)
      },
      log: (message) => logs.push(message),
    })

    await indexer.handleEvent({ type: "worktree.updated", data: { projectID: "project-service-api" } })
    await indexer.waitForIdle()

    expect(calls).toEqual(["/worktrees/fix-123"])
    expect(logs).toContain("finished zg index for worktree /worktrees/fix-123")
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
    const revision: ZvecGitRevision = { branch: "fix/new-route", commit: "new-commit" }
    const previous: ZvecIndexProvenance = {
      version: 1,
      root: "/worktrees/stale-branch",
      branch: "main",
      commit: "old-commit",
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
        saved = { version: 1, root: directory, ...value }
      },
      runIndex: async (directory, rebuild) => calls.push({ directory, rebuild }),
      log: () => {},
    })

    await indexer.refresh()
    await indexer.waitForIdle()

    expect(calls).toEqual([{ directory: "/worktrees/stale-branch", rebuild: true }])
    expect(saved).toEqual({ version: 1, root: "/worktrees/stale-branch", ...revision })
    indexer.dispose()
  })

  test("does not rebuild again when the recorded Git provenance matches", async () => {
    const revision: ZvecGitRevision = { branch: "main", commit: "same-commit" }
    const provenance: ZvecIndexProvenance = {
      version: 1,
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
      { branch: "fix/new-route", commit: "new-commit" },
      { branch: "fix/new-route", commit: "new-commit" },
      { branch: "fix/new-route", commit: "newer-commit" },
      { branch: "fix/new-route", commit: "newer-commit" },
      { branch: "fix/new-route", commit: "newer-commit" },
    ]
    const previous: ZvecIndexProvenance = {
      version: 1,
      root: "/worktrees/branch-race",
      branch: "main",
      commit: "old-commit",
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
      writeProvenance: async (directory, revision) => saved.push({ version: 1, root: directory, ...revision }),
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
    expect(saved).toEqual([{ version: 1, root: "/worktrees/branch-race", ...revisions[2] }])
    indexer.dispose()
  })
})
