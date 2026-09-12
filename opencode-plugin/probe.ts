// Pure dedupe + probe helpers for option A (probe-once, append-only).
// Kept dependency-free except for an injectable fetch so it's unit-testable;
// index.ts wires it to `tool.execute.after`.
//
// Policy: max 1 nudge per (session, normalized query), max 3 nudges per
// session total. Probe results are cached per query (10 min TTL) so a
// repeated grep doesn't re-hit the gateway. Messaging stays engram-only.

export function normalizeSearchKey(cli: string, query: string): string {
  const q = query.trim().replace(/\s+/g, " ").slice(0, 80).toLowerCase()
  return `${cli.toLowerCase()}::${q}`
}

export class NudgeDedupe {
  private seenQuery = new Map<string, number>()
  private perSession = new Map<string, number>()
  private maxPerQueryPerSession: number
  private maxPerSession: number
  constructor(maxPerQueryPerSession = 1, maxPerSession = 3) {
    this.maxPerQueryPerSession = maxPerQueryPerSession
    this.maxPerSession = maxPerSession
  }

  /** Returns true if this (session, key) may nudge; records the claim. */
  claim(sessionID: string, key: string): boolean {
    if (!sessionID || !key) return false
    const qk = `${sessionID}\0${key}`
    if ((this.seenQuery.get(qk) || 0) >= this.maxPerQueryPerSession) return false
    if ((this.perSession.get(sessionID) || 0) >= this.maxPerSession) return false
    this.seenQuery.set(qk, (this.seenQuery.get(qk) || 0) + 1)
    this.perSession.set(sessionID, (this.perSession.get(sessionID) || 0) + 1)
    return true
  }
}

export interface ProbeResult {
  ok: boolean
  hits: number
}

const PROBE_TTL_MS = 10 * 60 * 1000

export class ProbeCache {
  private cache = new Map<string, { at: number; result: ProbeResult }>()
  get(key: string, now = Date.now()): ProbeResult | null {
    const e = this.cache.get(key)
    if (!e) return null
    if (now - e.at > PROBE_TTL_MS) {
      this.cache.delete(key)
      return null
    }
    return e.result
  }
  set(key: string, result: ProbeResult, now = Date.now()): void {
    this.cache.set(key, { at: now, result })
  }
}

type FetchFn = (
  url: string,
  init: Record<string, unknown>,
) => Promise<{ ok: boolean; json(): Promise<unknown>; text(): Promise<string> }>

function toolArgsFor(toolName: string, query: string): Array<Record<string, unknown>> {
  // Match the tool's required params first (live schemas: code_search wants
  // {query}, pattern_search wants {pattern}, recall wants {query}).
  // No backend names leak — these are generic parameter spellings.
  if (/pattern/i.test(toolName)) return [{ pattern: query }, { query }, { q: query }]
  return [{ query }, { pattern: query }, { q: query }, { text: query }]
}

/** Parse MCP JSON-RPC over plain JSON or SSE (`event: message` + `data:`). */
async function readMcpJson(res: { json(): Promise<unknown>; text(): Promise<string> }): Promise<unknown> {
  const text = await res.text()
  const t = text.trim()
  if (!t) return null
  if (!/^event:/m.test(t)) return JSON.parse(t)
  const datas = t
    .split("\n")
    .filter((l) => l.startsWith("data:"))
    .map((l) => l.slice(5).trim())
    .filter(Boolean)
  if (datas.length === 0) return null
  return JSON.parse(datas[0])
}

function countHits(result: unknown): number {
  try {
    const text = JSON.stringify(result)
    if (!text || text.length < 32) return 0
    if (/isError["']?\s*:\s*true/i.test(text)) return 0
    // Count content items if present, else binary: non-empty => >=1.
    const m = text.match(/"text"\s*:/g)
    return m ? m.length : 1
  } catch {
    return 0
  }
}

/** Probe the gateway MCP endpoint: list tools, pick a search tool, call it. */
export async function probeGateway(
  gatewayUrl: string,
  query: string,
  fetchFn: FetchFn,
  timeoutMs = 2500,
): Promise<ProbeResult> {
  if (!query.trim()) return { ok: false, hits: 0 }
  const run = async (): Promise<ProbeResult> => {
    const listBody = { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }
    const listRes = await fetchFn(gatewayUrl, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
      body: JSON.stringify(listBody),
    })
    if (!listRes.ok) return { ok: false, hits: 0 }
    const listJson = (await readMcpJson(listRes)) as {
      result?: { tools?: Array<{ name?: string }> }
    }
    const names = (listJson?.result?.tools || []).map((t) => t.name || "").filter(Boolean)
    // Spike finding (2026-09-12, live gateway): the `code` backend behind
    // code_search/pattern_search is down (`is_error` compat), while recall
    // works. Try every search/recall tool in turn, not just the first.
    const candidates = [
      ...names.filter((n) => /search/i.test(n)),
      ...names.filter((n) => /recall/i.test(n)),
    ]
    if (candidates.length === 0) return { ok: false, hits: 0 }
    for (const pick of candidates) {
      for (const args of toolArgsFor(pick, query)) {
        try {
          const callRes = await fetchFn(gatewayUrl, {
            method: "POST",
            headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
            body: JSON.stringify({
              jsonrpc: "2.0",
              id: 2,
              method: "tools/call",
              params: { name: pick, arguments: args },
            }),
          })
          if (!callRes.ok) continue
          const hits = countHits(await readMcpJson(callRes))
          if (hits > 0) return { ok: true, hits }
          // isError / empty: try the next tool, it may be healthy.
          if (hits === 0) break
        } catch {
          continue
        }
      }
    }
    return { ok: false, hits: 0 }
  }
  return await Promise.race([
    run(),
    new Promise<ProbeResult>((resolve) => setTimeout(() => resolve({ ok: false, hits: 0 }), timeoutMs)),
  ])
}

export function buildProbedNudge(cli: string, query: string, hits: number): string {
  const q = query ? ` for \`${query.slice(0, 80)}\`` : ""
  const ev = hits > 0 ? ` (Engram has ${hits}+ hit${hits === 1 ? "" : "s"}${query ? ` for \`${query.slice(0, 60)}\`` : ""})` : ""
  return (
    `[engram-plugin] Detected \`${cli}\` code search${q} — ` +
    `Engram MCP looks useful here${ev}: use the engram gateway code-search${q} next time ` +
    `for methodology-aware results.`
  )
}
