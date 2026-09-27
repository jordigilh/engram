import { describe, expect, test } from "bun:test"
import {
  buildZvecIndexArgs,
  createKubernautWorktreeIndexer,
  shouldAutoIndexKubernautWorktrees,
} from "./worktree-index"

describe("Kubernaut worktree indexing policy", () => {
  test("is enabled only for the Kubernaut route unless explicitly disabled", () => {
    expect(shouldAutoIndexKubernautWorktrees("kubernaut")).toBe(true)
    expect(shouldAutoIndexKubernautWorktrees("kubernaut", false)).toBe(false)
    expect(shouldAutoIndexKubernautWorktrees("kubernaut-v1.5")).toBe(false)
    expect(shouldAutoIndexKubernautWorktrees("engram")).toBe(false)
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
})

describe("createKubernautWorktreeIndexer", () => {
  test("indexes unindexed siblings after matching worktree updates, but not the canonical checkout", async () => {
    const indexed = new Set<string>(["/worktrees/already-indexed"])
    const calls: string[] = []
    const logs: string[] = []
    const indexer = createKubernautWorktreeIndexer({
      projectID: "project-kubernaut",
      canonicalDirectory: "/repos/kubernaut",
      listWorktrees: async () => [
        { directory: "/repos/kubernaut" },
        { directory: "/worktrees/fix-123" },
        { directory: "/worktrees/already-indexed" },
      ],
      hasManifest: async (directory) => indexed.has(directory),
      runIndex: async (directory) => {
        calls.push(directory)
        indexed.add(directory)
      },
      log: (message) => logs.push(message),
    })

    await indexer.handleEvent({ type: "worktree.updated", data: { projectID: "project-kubernaut" } })
    await indexer.waitForIdle()

    expect(calls).toEqual(["/worktrees/fix-123"])
    expect(logs).toContain("finished zg index for Kubernaut worktree /worktrees/fix-123")
    indexer.dispose()
  })

  test("ignores updates from other projects", async () => {
    let listCalls = 0
    const indexer = createKubernautWorktreeIndexer({
      projectID: "project-kubernaut",
      canonicalDirectory: "/repos/kubernaut",
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
    const indexer = createKubernautWorktreeIndexer({
      projectID: "project-kubernaut",
      canonicalDirectory: "/repos/kubernaut",
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
      indexer.handleEvent({ type: "worktree.updated", data: { projectID: "project-kubernaut" } }),
      indexer.handleEvent({ type: "worktree.updated", data: { projectID: "project-kubernaut" } }),
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
    const indexer = createKubernautWorktreeIndexer({
      projectID: "project-kubernaut",
      canonicalDirectory: "/repos/kubernaut",
      listWorktrees: async () => [{ directory: "/worktrees/retry-me" }],
      hasManifest: async (directory) => indexed.has(directory),
      runIndex: async (directory) => {
        calls.push(directory)
        if (calls.length === 1) throw new Error("temporary zg failure")
        indexed.add(directory)
      },
      log: () => {},
    })

    const event = { type: "worktree.updated", data: { projectID: "project-kubernaut" } }
    await indexer.handleEvent(event)
    await indexer.waitForIdle()
    await indexer.handleEvent(event)
    await indexer.waitForIdle()

    expect(calls).toEqual(["/worktrees/retry-me", "/worktrees/retry-me"])
    indexer.dispose()
  })
})
