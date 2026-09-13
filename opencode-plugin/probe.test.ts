import { describe, expect, test } from "bun:test"
import { normalizeSearchKey, NudgeDedupe, ProbeCache, probeGateway, buildProbedNudge } from "./probe"

describe("NudgeDedupe", () => {
  test("one nudge per query per session, max three per session", () => {
    const d = new NudgeDedupe()
    const k = normalizeSearchKey("grep", "TODO")
    expect(d.claim("s1", k)).toBe(true)
    expect(d.claim("s1", k)).toBe(false) // same query repeat: silent
    expect(d.claim("s1", normalizeSearchKey("grep", "FIXME"))).toBe(true)
    expect(d.claim("s1", normalizeSearchKey("rg", "other"))).toBe(true)
    expect(d.claim("s1", normalizeSearchKey("grep", "fourth"))).toBe(false) // session cap
    expect(d.claim("s2", k)).toBe(true) // other session unaffected
  })

  test("normalization collapses whitespace/case", () => {
    expect(normalizeSearchKey("grep", "  TODO  ")).toBe(normalizeSearchKey("GREP", "todo"))
    expect(normalizeSearchKey("bash", "a  b")).toBe(normalizeSearchKey("bash", "a b"))
  })
})

describe("ProbeCache", () => {
  test("ttl expiry", () => {
    const c = new ProbeCache()
    c.set("k", { ok: true, hits: 2 }, 1000)
    expect(c.get("k", 2000)?.hits).toBe(2)
    expect(c.get("k", 1000 + 11 * 60 * 1000)).toBeNull()
  })
})

describe("probeGateway", () => {
  const sse = (obj: unknown) => `event: message\ndata: ${JSON.stringify(obj)}\n\n`
  const mockRes = (obj: unknown) => ({
    ok: true,
    json: async () => obj,
    text: async () => sse(obj),
  })

  test("returns hits on first successful arg shape", async () => {
    let calls = 0
    const fetchFn = async () => {
      calls++
      if (calls === 1) {
        return mockRes({ result: { tools: [{ name: "code_search" }] } })
      }
      return mockRes({ result: { content: [{ text: "hit" }] } })
    }
    const r = await probeGateway("http://x/mcp/engram", "TODO", fetchFn as never, 1000)
    expect(r.ok).toBe(true)
    expect(r.hits).toBeGreaterThan(0)
  })

  test("falls through a broken search tool to a healthy recall tool", async () => {
    const fetchFn = async (_url: string, init: Record<string, unknown>) => {
      const body = JSON.parse((init as { body: string }).body)
      if (body.method === "tools/list") {
        return mockRes({ result: { tools: [{ name: "code_search" }, { name: "recall" }] } })
      }
      if (body.params.name === "code_search") {
        return mockRes({ result: { content: [{ text: "backend failed" }], isError: true } })
      }
      return mockRes({ result: { content: [{ text: "hit" }] } })
    }
    const r = await probeGateway("http://x", "compaction hook", fetchFn as never, 1000)
    expect(r.ok).toBe(true)
    expect(r.hits).toBeGreaterThan(0)
  })

  test("silent when no search tool or gateway down", async () => {
    const noSearch = async () => mockRes({ result: { tools: [{ name: "other" }] } })
    expect((await probeGateway("http://x", "q", noSearch as never, 500)).ok).toBe(false)
    const down = async () => ({ ok: false, json: async () => ({}), text: async () => "" })
    expect((await probeGateway("http://x", "q", down as never, 500)).ok).toBe(false)
    expect((await probeGateway("http://x", "  ", down as never, 500)).ok).toBe(false)
  })

  test("probed nudge is engram-only", () => {
    const msg = buildProbedNudge("grep", "TODO", 3)
    expect(msg).toMatch(/engram/i)
    expect(msg).not.toMatch(/hindsight|cocoindex|serena/i)
  })
})
