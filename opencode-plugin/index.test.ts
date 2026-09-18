import { describe, expect, test } from "bun:test"
import { EngramPlugin } from "./index"

const fake$ = Object.assign(
  (_strings: TemplateStringsArray, ..._values: unknown[]) => ({
    cwd: (_directory: string) => ({
      quiet: () => ({ text: async () => "" }),
    }),
  }),
  {},
)

function context(sessions: Record<string, { parentID?: string | null }>) {
  return {
    client: {
      session: {
        get: async ({ path }: { path: { id: string } }) => {
          const data = sessions[path.id]
          if (!data) throw new Error("not found")
          return { data }
        },
      },
    },
    project: {},
    directory: "/workspace/engram",
    worktree: "/workspace/engram",
    experimental_workspace: { register: () => {} },
    serverUrl: new URL("http://127.0.0.1:4096"),
    $: fake$,
  }
}

describe("EngramPlugin Engram-first hooks", () => {
  test("denies non-Engram permissions and tool execution for children", async () => {
    const hooks = await EngramPlugin(
      context({ child: { parentID: "primary" } }) as never,
      {},
    )
    const permission = { status: "allow" as const }

    await hooks["permission.ask"]?.(
      { type: "bash", sessionID: "child" } as never,
      permission,
    )
    expect(permission.status).toBe("deny")

    await expect(
      hooks["tool.execute.before"]?.(
        { tool: "bash", sessionID: "child", callID: "call-1" },
        { args: { command: "pwd" } },
      ),
    ).rejects.toThrow("must call an Engram MCP tool")
  })

  test("unlocks a child only after the Engram after hook", async () => {
    const hooks = await EngramPlugin(
      context({ child: { parentID: "primary" } }) as never,
      {},
    )
    const before = hooks["tool.execute.before"]!
    const after = hooks["tool.execute.after"]!

    await expect(
      before(
        { tool: "read", sessionID: "child", callID: "call-1" },
        { args: {} },
      ),
    ).rejects.toThrow()

    await after(
      { tool: "engram_docs_recall", sessionID: "child", callID: "call-2", args: {} },
      { title: "Recall", output: "ok", metadata: {} },
    )

    await expect(
      before({ tool: "read", sessionID: "child", callID: "call-3" }, { args: {} }),
    ).resolves.toBeUndefined()
  })

  test("leaves primary sessions unaffected", async () => {
    const hooks = await EngramPlugin(
      context({ primary: {} }) as never,
      {},
    )
    const permission = { status: "allow" as const }

    await hooks["permission.ask"]?.(
      { type: "bash", sessionID: "primary" } as never,
      permission,
    )
    expect(permission.status).toBe("allow")
    await expect(
      hooks["tool.execute.before"]?.(
        { tool: "bash", sessionID: "primary", callID: "call-1" },
        { args: {} },
      ),
    ).resolves.toBeUndefined()
  })
})
