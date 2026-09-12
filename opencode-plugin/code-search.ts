// Pure helper for the code-search nudge. Kept dependency-free so it's
// directly unit-testable; index.ts wires it to `tool.execute.before`
// (operator log) and `tool.execute.after` (LLM-visible hint).
//
// Scope: read-only code search only — grep/rg/ag/ack, git grep, awk without
// gawk inplace, sed -n (print-only), find -name, and the built-in grep/glob
// tools. Replace operations (sed -i, perl -pi, edit/write/apply_patch) are
// never matched. Messaging is engram-only: never names backend servers.

export interface CodeSearchHit {
  cli: string
  query: string
}

function truncate(s: string, max = 80): string {
  const t = s.trim().replace(/^['"]+|['"]+$/g, "")
  return t.length > max ? `${t.slice(0, max - 1)}…` : t
}

function tokens(command: string): string[] {
  // Quote-aware split: 'my query' stays one token so query extraction keeps
  // the full phrase. Shell separators outside quotes still split.
  const out: string[] = []
  let cur = ""
  let quote: string | null = null
  for (const ch of command) {
    if (quote) {
      if (ch === quote) quote = null
      else cur += ch
    } else if (ch === '"' || ch === "'") {
      quote = ch
    } else if (/[\s;&|()<>`]/.test(ch)) {
      if (cur) {
        out.push(cur)
        cur = ""
      }
    } else {
      cur += ch
    }
  }
  if (cur) out.push(cur)
  return out.filter(Boolean)
}

function queryAfter(parts: string[], idx: number): string {
  for (let i = idx + 1; i < parts.length; i++) {
    const p = parts[i]
    if (!p || p.startsWith("-")) continue
    // Skip long option values like --glob=*.ts (keep the value).
    const eq = p.indexOf("=")
    if (p.startsWith("--") && eq > 0) return truncate(p.slice(eq + 1))
    // Skip paths that look like flags' values for common search flags.
    if (["-e", "-E", "-m", "--glob", "-g", "-t", "--type"].includes(parts[i - 1] || "")) {
      return truncate(p)
    }
    return truncate(p)
  }
  return ""
}

function detectBashSearch(command: string): CodeSearchHit | null {
  // Replace operations: never nudge.
  if (/\bsed\b[^|&;]*\s-i\b/.test(command)) return null
  if (/\bperl\b[^|&;]*\s-i/.test(command)) return null

  const parts = tokens(command)
  const lower = parts.map((p) => p.toLowerCase())

  // git grep (two-token binary)
  const gitIdx = lower.findIndex((p, i) => p === "git" && lower[i + 1] === "grep")
  if (gitIdx >= 0) return { cli: "git grep", query: queryAfter(parts, gitIdx + 1) }

  const binaries = ["grep", "rg", "ag", "ack"]
  for (let i = 0; i < lower.length; i++) {
    const base = lower[i].split("/").pop() || ""
    if (binaries.includes(base)) return { cli: base, query: queryAfter(parts, i) }
  }

  // awk without gawk inplace (-i inplace)
  const awkIdx = lower.findIndex((p) => (p.split("/").pop() || "") === "awk")
  if (awkIdx >= 0) {
    const window = parts.slice(Math.max(0, awkIdx - 2), awkIdx + 3).join(" ")
    if (/(^|\s)-i(\s|$)/.test(window) || /inplace/.test(window)) return null
    return { cli: "awk", query: queryAfter(parts, awkIdx) }
  }

  // sed -n (print-only view)
  const sedIdx = lower.findIndex((p) => (p.split("/").pop() || "") === "sed")
  if (sedIdx >= 0) {
    const window = parts.slice(sedIdx, sedIdx + 4).join(" ")
    if (/(^|\s)-n(\s|$)/.test(window)) return { cli: "sed -n", query: queryAfter(parts, sedIdx) }
    return null
  }

  // find ... -name / -iname (file search)
  const findIdx = lower.findIndex((p) => (p.split("/").pop() || "") === "find")
  if (findIdx >= 0 && /-i?name\b/.test(command)) {
    return { cli: "find", query: queryAfter(parts, findIdx) }
  }

  return null
}

export function detectCodeSearch(tool: string, args: unknown): CodeSearchHit | null {
  // Never nudge Engram's own tools.
  if (tool.toLowerCase().includes("engram")) return null
  // Replace tools: never nudge.
  if (tool === "edit" || tool === "write" || tool === "apply_patch") return null

  if (tool === "grep") {
    const pattern =
      (args as { pattern?: unknown } | null)?.pattern ??
      (args as { regex?: unknown } | null)?.regex
    return { cli: "grep", query: typeof pattern === "string" ? truncate(pattern) : "" }
  }
  if (tool === "glob") {
    const pattern = (args as { pattern?: unknown } | null)?.pattern
    return { cli: "glob", query: typeof pattern === "string" ? truncate(pattern) : "" }
  }

  if (tool === "bash") {
    const command = (args as { command?: unknown } | null)?.command
    if (typeof command !== "string" || command.length === 0) return null
    return detectBashSearch(command)
  }

  return null
}

export function buildCodeSearchNudge(hit: CodeSearchHit): string {
  const q = hit.query ? ` for \`${hit.query}\`` : ""
  return (
    `[engram-plugin] Detected \`${hit.cli}\` code search${q} — ` +
    `consider Engram MCP instead: use the engram gateway code-search${q} next time ` +
    `for methodology-aware results.`
  )
}
