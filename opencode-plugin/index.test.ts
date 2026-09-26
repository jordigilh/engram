import { describe, expect, test } from "bun:test"
import plugin from "./index"

function fakeContext() {
  const toolHooks: Record<string, Array<(event: any) => void | Promise<void>>> = {}
  const context: any = {
    location: { directory: "/workspace/engram" },
    options: {},
    mcp: {
      transform: async (callback: (editor: any) => void) =>
        callback({ get: () => undefined, set: () => {} }),
    },
    session: { hook: async () => ({ dispose: async () => {} }) },
    tool: {
      hook: async (name: string, callback: (event: any) => void | Promise<void>) => {
        ;(toolHooks[name] ??= []).push(callback)
        return { dispose: async () => {} }
      },
    },
    event: { subscribe: async function* () {} },
  }
  return { context, toolHooks }
}

describe("V2 plugin entrypoint", () => {
  test("exports the V2 setup API without the V1 server entrypoint", () => {
    expect(typeof plugin.setup).toBe("function")
    expect("server" in plugin).toBe(false)
  })

  test("keeps hooks non-blocking for unrelated and failed tool calls", async () => {
    const { context, toolHooks } = fakeContext()
    await plugin.setup(context)

    expect(
      toolHooks["execute.before"][0]({ tool: "read", sessionID: "session-1", input: {} }),
    ).toBeUndefined()
    await expect(
      toolHooks["execute.after"][0]({
        tool: "grep",
        sessionID: "session-1",
        input: { pattern: "needle" },
        status: "error",
        error: { message: "tool failed" },
      }),
    ).resolves.toBeUndefined()
  })
})
