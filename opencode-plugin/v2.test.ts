import { describe, expect, test } from "bun:test"
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import * as path from "node:path"
import pluginDefault, { appendV2Nudge } from "./index"
import { buildMcpServerConfigV2, mergeMcpEditor } from "./identity"
import "./test-config"

function fakeEditor(initial: Record<string, unknown> = {}) {
  const store = new Map<string, unknown>(Object.entries(initial))
  return {
    store,
    get: (name: string) => store.get(name),
    set: (name: string, config: unknown) => {
      store.set(name, config)
    },
    update: (name: string, fn: (c: any) => void) => {
      const v = store.get(name) as any
      if (v) fn(v)
    },
    remove: (name: string) => {
      store.delete(name)
    },
  }
}

function fakeV2Ctx(mcpInitial: Record<string, unknown> = {}) {
  const editor = fakeEditor(mcpInitial)
  const transforms: Array<(editor: any) => void> = []
  const sessionHooks: Record<string, Array<(event: any) => void | Promise<void>>> = {}
  const toolHooks: Record<string, Array<(event: any) => void | Promise<void>>> = {}
  const ctx: any = {
    location: { directory: "/workspace/engram" },
    options: {},
    mcp: {
      transform: async (cb: (editor: any) => void) => {
        transforms.push(cb)
        cb(editor)
        return { dispose: async () => {} }
      },
      list: async () => ({ data: [{ name: "engram", status: "connected" }] }),
    },
    session: {
      hook: async (name: string, cb: (event: any) => void | Promise<void>) => {
        ;(sessionHooks[name] = sessionHooks[name] || []).push(cb)
        return { dispose: async () => {} }
      },
    },
    tool: {
      hook: async (name: string, cb: (event: any) => void | Promise<void>) => {
        ;(toolHooks[name] = toolHooks[name] || []).push(cb)
        return { dispose: async () => {} }
      },
    },
    event: {
      subscribe: async function* (_opts?: { signal?: AbortSignal }) {},
    },
  }
  return { ctx, editor, sessionHooks, toolHooks }
}

async function initializeGitIdentity(directory: string, branch: string, remote?: string): Promise<void> {
  const run = async (args: string[]): Promise<void> => {
    const child = Bun.spawn(["git", "-C", directory, ...args], { stdout: "ignore", stderr: "ignore" })
    const exitCode = await child.exited
    if (exitCode !== 0) throw new Error(`git ${args.join(" ")} failed in ${directory}`)
  }
  await mkdir(directory, { recursive: true })
  await run(["init", "-q"])
  await run(["symbolic-ref", "HEAD", `refs/heads/${branch}`])
  await writeFile(path.join(directory, ".gitignore"), ".zvec-grep/\n")
  await writeFile(path.join(directory, "fixture.txt"), "fixture\n")
  if (remote) await run(["remote", "add", "origin", remote])
  await run(["add", ".gitignore", "fixture.txt"])
  await run([
    "-c", "user.name=Engram Test",
    "-c", "user.email=engram-test@example.com",
    "commit", "-qm", "fixture",
  ])
}

describe("V2 plugin export", () => {
  test("default export exposes only the V2 setup entrypoint", () => {
    const def = pluginDefault as unknown as Record<string, unknown>
    expect(typeof def["setup"]).toBe("function")
    expect(def).not.toHaveProperty("server")
  })
})

describe("buildMcpServerConfigV2", () => {
  test("emits a native V2 remote entry with OAuth disabled", () => {
    const cfg = buildMcpServerConfigV2(
      { project: "service-api-v1.5" },
      { gatewayUrl: "http://127.0.0.1:8896" },
    )
    expect(cfg).toEqual({
      type: "remote",
      url: "http://127.0.0.1:8896/mcp/service-api-v1.5",
      oauth: false,
      disabled: false,
      codemode: false,
    })
  })

  test("honors gatewayUrl override without using family for the route", () => {
    const cfg = buildMcpServerConfigV2({ project: "myrepo" }, { gatewayUrl: "http://localhost:9999/" })
    expect(cfg.url).toBe("http://localhost:9999/mcp/myrepo")
  })

  test("does not invent a gateway endpoint when deployment config is absent", () => {
    expect(buildMcpServerConfigV2({ project: "service-api" })).toBeUndefined()
  })
})

describe("mergeMcpEditor", () => {
  test("adds the generated route when none exists", () => {
    const editor = fakeEditor()
    const generated = buildMcpServerConfigV2({ project: "engram" }, { gatewayUrl: "http://127.0.0.1:8896" })!
    mergeMcpEditor(editor as never, generated)
    expect(editor.store.get("engram")).toEqual(generated)
  })

  test("never overwrites an explicit route", () => {
    const explicit = { type: "remote", url: "http://127.0.0.1:8896/mcp/dcm", oauth: false, disabled: false }
    const editor = fakeEditor({ engram: explicit })
    mergeMcpEditor(
      editor as never,
      buildMcpServerConfigV2({ project: "other" }, { gatewayUrl: "http://127.0.0.1:8896" })!,
    )
    expect(editor.store.get("engram")).toEqual(explicit)
  })
})

describe("v2 setup", () => {
  test("registers a fallback engram MCP entry", async () => {
    const { ctx, editor } = fakeV2Ctx()
    ctx.options = { gatewayUrl: "http://127.0.0.1:8896", project: "engram" }
    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    const cleanup = await def.setup(ctx)
    expect(editor.store.get("engram")).toEqual({
      type: "remote",
      url: "http://127.0.0.1:8896/mcp/engram",
      oauth: false,
      disabled: false,
      codemode: false,
    })
    expect(typeof cleanup).toBe("function")
    ;(cleanup as () => void)()
  })

  test("does not invent a route for an unconfigured non-repository location", async () => {
    const { ctx, editor } = fakeV2Ctx()
    ctx.location = { directory: "/workspace/openchamber-chat" }
    ctx.options = { gatewayUrl: "http://127.0.0.1:8896" }
    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    await def.setup(ctx)

    expect(editor.store.has("engram")).toBe(false)
  })

  test("preserves an explicit engram route from config", async () => {
    const explicit = { type: "remote", url: "http://127.0.0.1:8896/mcp/dcm", oauth: false, disabled: false }
    const { ctx, editor } = fakeV2Ctx({ engram: explicit })
    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    await def.setup(ctx)
    expect(editor.store.get("engram")).toEqual(explicit)
  })

  test("keeps hooks active without inventing a route when gateway config is absent", async () => {
    const { ctx, editor, sessionHooks } = fakeV2Ctx()
    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    await def.setup(ctx)

    expect(editor.store.has("engram")).toBe(false)
    expect(sessionHooks["context"]?.length).toBe(1)
  })

  test("context hook re-applies methodology recall to every request", async () => {
    const { ctx, sessionHooks } = fakeV2Ctx()
    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    await def.setup(ctx)
    expect(sessionHooks["context"]?.length).toBe(1)
    expect(sessionHooks["compaction"]?.length).toBe(1)
    const event: any = { system: [] }
    await sessionHooks["context"][0](event)
    expect(event.system.length).toBe(1)
    expect(event.system[0].text).toContain("engram")
  })

  test("tool before-hook never blocks and nudges engram CLI use", async () => {
    const { ctx, toolHooks } = fakeV2Ctx()
    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    await def.setup(ctx)
    expect(toolHooks["execute.before"]?.length).toBe(1)
    expect(toolHooks["execute.after"]?.length).toBe(1)
    await toolHooks["execute.before"][0]({ tool: "bash", sessionID: "s1", input: { command: "engram recall" } })
    await toolHooks["execute.before"][0]({ tool: "read", sessionID: "s1", input: {} })
  })

  test("configured worktree indexing uses the canonical route and handles worktree.updated", async () => {
    const { ctx } = fakeV2Ctx()
    ctx.location = {
      directory: "/worktrees/fix-123",
      project: { id: "project-service-api", canonical: "/repos/service-api" },
    }
    ctx.options = { worktreeIndex: { projects: ["service-api"] } }
    let listCalls = 0
    let resolveSecondList: (() => void) | undefined
    const secondList = new Promise<void>((resolve) => {
      resolveSecondList = resolve
    })
    ctx.worktree = {
      list: async ({ projectID }: { projectID: string }) => {
        expect(projectID).toBe("project-service-api")
        listCalls += 1
        if (listCalls >= 2) resolveSecondList?.()
        return []
      },
    }
    ctx.event.subscribe = async function* () {
      yield { type: "worktree.updated", data: { projectID: "project-service-api" } }
    }

    let timeout: ReturnType<typeof setTimeout> | undefined
    const timeoutPromise = new Promise<void>((_resolve, reject) => {
      timeout = setTimeout(
        () => reject(new Error("worktree.updated did not trigger an inventory refresh")),
        1000,
      )
    })
    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    let cleanup: (() => void) | void = undefined
    try {
      cleanup = await def.setup(ctx)
      await Promise.race([secondList, timeoutPromise])
      expect(listCalls).toBeGreaterThanOrEqual(2)
    } finally {
      if (timeout) clearTimeout(timeout)
      if (typeof cleanup === "function") cleanup()
    }
  })

  test("does not gate prompts merely because background indexing is configured", async () => {
    const { ctx, sessionHooks } = fakeV2Ctx()
    ctx.location = {
      directory: "/worktrees/fix-123",
      project: { id: "project-service-api", canonical: "/repos/service-api" },
    }
    ctx.options = { worktreeIndex: { projects: ["service-api"] } }
    ctx.worktree = { list: async () => [] }

    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    const cleanup = await def.setup(ctx)
    expect(sessionHooks["prompt"]).toBeUndefined()
    if (typeof cleanup === "function") cleanup()
  })

  test("registers a prompt gate only when explicitly configured", async () => {
    const { ctx, sessionHooks } = fakeV2Ctx()
    ctx.location = {
      directory: "/worktrees/fix-123",
      project: { id: "project-service-api", canonical: "/repos/service-api" },
    }
    ctx.options = {
      project: "service-api",
      gatewayUrl: "http://127.0.0.1:8896",
      worktreeIndex: { projects: ["service-api"], gateSessions: true },
    }
    ctx.worktree = {
      list: async () => [],
    }

    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    const cleanup = await def.setup(ctx)
    expect(sessionHooks["prompt"]?.length).toBe(1)
    if (typeof cleanup === "function") cleanup()
  })

  test("preflights the exact root supplied to a zvec workspace read", async () => {
    const fixture = await mkdtemp(path.join(tmpdir(), "engram-zvec-preflight-"))
    const requestedRoot = path.join(fixture, "openchamber-worktree-pool")
    await mkdir(requestedRoot)

    const { ctx, toolHooks } = fakeV2Ctx()
    ctx.location = {
      directory: fixture,
      project: { id: "project-service-api", canonical: fixture },
    }
    ctx.options = {
      project: "service-api",
      gatewayUrl: "http://127.0.0.1:8896",
      worktreeIndex: {
        projects: ["service-api"],
        mcpUrl: "http://zvec.test/mcp",
        aggregateRoots: [requestedRoot],
      },
    }
    ctx.worktree = { list: async () => [] }

    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    let cleanup: (() => void) | void = undefined
    const previousFetch = globalThis.fetch
    globalThis.fetch = (async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as {
        method: string
        params?: { arguments?: { root?: string } }
      }
      if (request.method === "initialize") {
        return new Response('data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n', {
          headers: { "content-type": "text/event-stream", "mcp-session-id": "test-session" },
        })
      }
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      const root = request.params?.arguments?.root || requestedRoot
      await mkdir(path.join(root, ".zvec-grep"), { recursive: true })
      await writeFile(path.join(root, ".zvec-grep", "manifest.json"), "{}\n")
      return new Response('data: {"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"state":"succeeded"}}}\n\n', {
        headers: { "content-type": "text/event-stream", "mcp-session-id": "test-session" },
      })
    }) as typeof fetch
    try {
      cleanup = await def.setup(ctx)
      await toolHooks["execute.before"][0]({
        tool: "engram_zvec_grep_search",
        sessionID: "session-zvec-preflight",
        input: { root: requestedRoot, query: "worktree" },
      })
      expect(await Bun.file(path.join(requestedRoot, ".zvec-grep", "manifest.json")).exists()).toBe(true)
    } finally {
      globalThis.fetch = previousFetch
      if (typeof cleanup === "function") cleanup()
      await rm(fixture, { recursive: true, force: true })
    }
  })

  test("uses the canonical route family for sibling authorization and rejects unrelated roots", async () => {
    const fixture = await mkdtemp(path.join(tmpdir(), "engram-family-root-preflight-"))
    const canonical = path.join(fixture, "service-api")
    const worktree = path.join(canonical, "worktree")
    const familyRoot = path.join(fixture, "service-ui")
    const unrelatedRoot = path.join(fixture, "unrelated")
    await mkdir(worktree, { recursive: true })
    await initializeGitIdentity(familyRoot, "main")
    await mkdir(unrelatedRoot)

    const { ctx, toolHooks } = fakeV2Ctx()
    ctx.location = {
      directory: worktree,
      project: { id: "project-service-api", canonical },
    }
    ctx.options = {
      repositories: {
        directories: {
          "service-api": { project: "service-api", family: "platform" },
        },
      },
      worktreeIndex: {
        projects: ["service-api"],
        mcpUrl: "http://zvec.test/mcp",
        familyRoots: { platform: [familyRoot] },
      },
    }
    ctx.worktree = { list: async () => [] }

    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    let cleanup: (() => void) | void = undefined
    const previousFetch = globalThis.fetch
    globalThis.fetch = (async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as {
        method: string
        params?: { arguments?: { root?: string } }
      }
      if (request.method === "initialize") {
        return new Response('data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n', {
          headers: { "content-type": "text/event-stream", "mcp-session-id": "family-session" },
        })
      }
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      const root = request.params?.arguments?.root || familyRoot
      await mkdir(path.join(root, ".zvec-grep"), { recursive: true })
      await writeFile(path.join(root, ".zvec-grep", "manifest.json"), "{}\n")
      return new Response('data: {"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"state":"succeeded"}}}\n\n', {
        headers: { "content-type": "text/event-stream", "mcp-session-id": "family-session" },
      })
    }) as typeof fetch
    try {
      cleanup = await def.setup(ctx)
      await toolHooks["execute.before"][0]({
        tool: "engram_zvec_grep_search",
        sessionID: "session-family-root",
        input: { root: familyRoot, query: "shared family" },
      })
      await expect(toolHooks["execute.before"][0]({
        tool: "engram_zvec_grep_search",
        sessionID: "session-unrelated-root",
        input: { root: unrelatedRoot, query: "must fail closed" },
      })).rejects.toThrow("not authorized")
    } finally {
      globalThis.fetch = previousFetch
      if (typeof cleanup === "function") cleanup()
      await rm(fixture, { recursive: true, force: true })
    }
  })

  test("enables family preflight when a linked release worktree is the configured route", async () => {
    const fixture = await mkdtemp(path.join(tmpdir(), "engram-release-family-preflight-"))
    const canonical = path.join(fixture, "service-api")
    const worktree = path.join(fixture, "service-api-v1.5")
    const familyRoot = path.join(fixture, "service-ui")
    const unrelatedRoot = path.join(fixture, "unrelated")
    const remote = "https://example.com/service-api"
    await initializeGitIdentity(canonical, "main", remote)
    await initializeGitIdentity(worktree, "release/v1.5", remote)
    await initializeGitIdentity(familyRoot, "main")
    await mkdir(unrelatedRoot)

    const { ctx, toolHooks } = fakeV2Ctx()
    ctx.location = {
      directory: worktree,
      project: { id: "project-service-api", canonical },
    }
    ctx.options = {
      branchRoutes: { main: "service-api", "v1.5": "service-api-v1.5", "release/v1.5": "service-api-v1.5" },
      repositories: {
        remotes: {
          [remote]: { project: "service-api", family: "platform" },
        },
      },
      worktreeIndex: {
        projects: ["service-api-v1.5"],
        mcpUrl: "http://zvec.test/mcp",
        familyRoots: { platform: [familyRoot] },
      },
    }
    ctx.worktree = { list: async () => [] }

    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    let cleanup: (() => void) | void = undefined
    const previousFetch = globalThis.fetch
    globalThis.fetch = (async (_input, init) => {
      const request = JSON.parse(String(init?.body)) as {
        method: string
        params?: { arguments?: { root?: string } }
      }
      if (request.method === "initialize") {
        return new Response('data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n', {
          headers: { "content-type": "text/event-stream", "mcp-session-id": "release-family-session" },
        })
      }
      if (request.method === "notifications/initialized") return new Response(null, { status: 202 })
      const root = request.params?.arguments?.root || familyRoot
      await mkdir(path.join(root, ".zvec-grep"), { recursive: true })
      await writeFile(path.join(root, ".zvec-grep", "manifest.json"), "{}\n")
      return new Response('data: {"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"state":"succeeded"}}}\n\n', {
        headers: { "content-type": "text/event-stream", "mcp-session-id": "release-family-session" },
      })
    }) as typeof fetch
    try {
      cleanup = await def.setup(ctx)
      await toolHooks["execute.before"][0]({
        tool: "engram_zvec_grep_search",
        sessionID: "session-release-family-root",
        input: { root: familyRoot, query: "release family" },
      })
      await expect(toolHooks["execute.before"][0]({
        tool: "engram_zvec_grep_search",
        sessionID: "session-release-unrelated-root",
        input: { root: unrelatedRoot, query: "must fail closed" },
      })).rejects.toThrow("not authorized")
    } finally {
      globalThis.fetch = previousFetch
      if (typeof cleanup === "function") cleanup()
      await rm(fixture, { recursive: true, force: true })
    }
  })
})

describe("appendV2Nudge", () => {
  test("appends to string content without duplicating", () => {
    let result: any = { content: "grep found x" }
    result = appendV2Nudge(result, "[engram-plugin] nudge")
    expect(result.content).toBe("grep found x\n\n[engram-plugin] nudge")
    result = appendV2Nudge(result, "[engram-plugin] nudge again")
    expect(result.content).toBe("grep found x\n\n[engram-plugin] nudge")
  })

  test("pushes a text part onto array content", () => {
    let result: any = { content: [{ type: "text", text: "done" }] }
    result = appendV2Nudge(result, "[engram-plugin] nudge")
    expect(result.content).toEqual([{ type: "text", text: "done" }, { type: "text", text: "[engram-plugin] nudge" }])
    result = appendV2Nudge(result, "[engram-plugin] duplicate")
    expect(result.content).toHaveLength(2)
  })

  test("fills missing content", () => {
    const result: any = appendV2Nudge({}, "[engram-plugin] nudge")
    expect(result.content).toBe("[engram-plugin] nudge")
  })

  test("ignores an absent result", () => {
    expect(() => appendV2Nudge(undefined, "[engram-plugin] nudge")).not.toThrow()
  })
})
