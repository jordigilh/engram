// Spike harness: exercises the REAL plugin wiring (detect → live probe →
// dedupe → append) without an LLM. Run manually against a live gateway:
//   ENGRAM_METRICS_PATH=~/.engram/logs/dedupe-spike-metrics.jsonl bun opencode-plugin/dedupe-spike.ts
// Not a CI test (needs live :8896); do not rename to *.test.ts.
import plugin from "./index"

const toolHooks: Record<string, (event: any) => void | Promise<void>> = {}
const ctx: any = {
  location: { directory: process.cwd() },
  options: {},
  mcp: {
    transform: async (callback: (editor: any) => void) => callback({ get: () => undefined, set: () => {} }),
  },
  session: { hook: async () => ({ dispose: async () => {} }) },
  tool: {
    hook: async (name: string, callback: (event: any) => void | Promise<void>) => {
      toolHooks[name] = callback
      return { dispose: async () => {} }
    },
  },
  event: { subscribe: async function* () {} },
}

await plugin.setup(ctx)
const after = toolHooks["execute.after"]
if (!after) throw new Error("tool.execute.after not wired")

const runAfter = async (sessionID: string, id: string, input: unknown) => {
  const event = { tool: "grep", sessionID, id, input, status: "completed", result: { content: "TODO: fix\n" } }
  await after(event)
  return event.result.content.includes("[engram-plugin]")
}

const q = { pattern: "compaction hook" }
const first = await runAfter("spike-s1", "c1", q)
const second = await runAfter("spike-s1", "c2", q)
const otherSession = await runAfter("spike-s2", "c3", q)
const otherQuery = await runAfter("spike-s1", "c4", { pattern: "compaction hook probe" })

console.log(`first same-query nudges:        ${first ? "PASS" : "FAIL (no live hits or probe timeout)"}`)
console.log(`second same-query stays silent: ${!second ? "PASS" : "FAIL (loop!)"}`)
console.log(`other session nudges:           ${otherSession ? "PASS" : "FAIL"}`)
console.log(`different query nudges:         ${otherQuery ? "PASS (cap not hit)" : "INFO (silent — cap or no hits)"}`)

if (!first) process.exit(2)
if (second) process.exit(3)
console.log("dedupe spike: gap closed")
