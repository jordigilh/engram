import { describe, expect, test } from "bun:test"
import pluginDefault, { appendV2Nudge } from "./index"
import { buildMcpServerConfigV2, mergeMcpEditor } from "./identity"

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

describe("V2 plugin export", () => {
  test("default export exposes only the V2 setup entrypoint", () => {
    const def = pluginDefault as unknown as Record<string, unknown>
    expect(typeof def["setup"]).toBe("function")
    expect(def).not.toHaveProperty("server")
  })
})

describe("buildMcpServerConfigV2", () => {
  test("emits a native V2 remote entry with OAuth disabled", () => {
    const cfg = buildMcpServerConfigV2({ project: "service-api-v1.5" })
    expect(cfg).toEqual({
      type: "remote",
      url: "http://127.0.0.1:8896/mcp/service-api-v1.5",
      oauth: false,
      disabled: false,
    })
  })

  test("honors gatewayUrl override without using family for the route", () => {
    const cfg = buildMcpServerConfigV2({ project: "myrepo" }, { gatewayUrl: "http://localhost:9999/" })
    expect(cfg.url).toBe("http://localhost:9999/mcp/myrepo")
  })
})

describe("mergeMcpEditor", () => {
  test("adds the generated route when none exists", () => {
    const editor = fakeEditor()
    const generated = buildMcpServerConfigV2({ project: "engram" })
    mergeMcpEditor(editor as never, generated)
    expect(editor.store.get("engram")).toEqual(generated)
  })

  test("never overwrites an explicit route", () => {
    const explicit = { type: "remote", url: "http://127.0.0.1:8896/mcp/dcm", oauth: false, disabled: false }
    const editor = fakeEditor({ engram: explicit })
    mergeMcpEditor(editor as never, buildMcpServerConfigV2({ project: "other" }))
    expect(editor.store.get("engram")).toEqual(explicit)
  })
})

describe("v2 setup", () => {
  test("registers a fallback engram MCP entry", async () => {
    const { ctx, editor } = fakeV2Ctx()
    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    const cleanup = await def.setup(ctx)
    expect(editor.store.get("engram")).toEqual({
      type: "remote",
      url: "http://127.0.0.1:8896/mcp/engram",
      oauth: false,
      disabled: false,
    })
    expect(typeof cleanup).toBe("function")
    ;(cleanup as () => void)()
  })

  test("preserves an explicit engram route from config", async () => {
    const explicit = { type: "remote", url: "http://127.0.0.1:8896/mcp/dcm", oauth: false, disabled: false }
    const { ctx, editor } = fakeV2Ctx({ engram: explicit })
    const def = pluginDefault as unknown as { setup: (ctx: any) => Promise<(() => void) | void> }
    await def.setup(ctx)
    expect(editor.store.get("engram")).toEqual(explicit)
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
