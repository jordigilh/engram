// Pure helper for the MCP-over-CLI nudge. Kept dependency-free so it's
// directly unit-testable; index.ts wires it to `tool.execute.before`.
//
// Only matches explicit `engram` CLI invocations that should go through the
// gateway MCP instead. Backend names are intentionally never matched —
// users only ever see the `engram` gateway. Allowlist-based to avoid false
// positives: must be the `bash` tool and the command must reference `engram`
// as a standalone token.

const CLI_TOKENS = ["engram"]

function containsCliToken(command: string): string | null {
  // Split on shell separators and whitespace to avoid substring matches
  // (e.g. `my-engram-wrapper` should not nudge).
  const parts = command.split(/[\s;&|()<>`'"]+/).filter(Boolean)
  for (const part of parts) {
    const base = part.split("/").pop() || ""
    const name = base.split(".")[0] || ""
    if (CLI_TOKENS.includes(name) || CLI_TOKENS.includes(base)) return name
  }
  return null
}

export function shouldNudgeMcp(tool: string, command: unknown): string | null {
  if (tool !== "bash") return null
  if (typeof command !== "string" || command.length === 0) return null
  return containsCliToken(command)
}

export function buildMcpNudgeMessage(cliName: string): string {
  return `Prefer Engram MCP over CLI: detected \`${cliName}\` via bash — use the engram gateway MCP tools for recall/retain/code-search when possible.`
}
