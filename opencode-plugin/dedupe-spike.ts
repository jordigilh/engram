// Spike harness: exercises the REAL plugin wiring (detect → live probe →
// dedupe → append) without an LLM. Run manually against a live gateway:
//   ENGRAM_METRICS_PATH=/tmp/spike-metrics.jsonl bun opencode-plugin/dedupe-spike.ts
// Not a CI test (needs live :8896); do not rename to *.test.ts.
import { EngramPlugin } from "./index"

const DIR = "/Users/jgil/go/src/github.com/jordigilh/engram"

// Minimal $ stub: only `git rev-parse --abbrev-ref HEAD` is used.
const fake$ = Object.assign(
  (_strings: TemplateStringsArray, ..._values: unknown[]) => ({
    cwd: (_d: string) => ({
      quiet: () => ({ text: async () => "main\n" }),
    }),
  }),
  {},
)

const ctx = {
  client: {},
  project: {},
  directory: DIR,
  worktree: DIR,
  experimental_workspace: { register: () => {} },
  serverUrl: new URL("http://127.0.0.1:4096"),
  $: fake$,
}

const hooks = await EngramPlugin(ctx as never, {})
const after = hooks["tool.execute.after"]
if (!after) throw new Error("tool.execute.after not wired")

const runAfter = async (sessionID: string, callID: string, args: unknown) => {
  const output = { title: "grep", output: "TODO: fix\n", metadata: {} }
  await after({ tool: "grep", sessionID, callID, args } as never, output as never)
  return output.output.includes("[engram-plugin]")
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
