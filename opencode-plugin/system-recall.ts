// Pure helper for the every-prompt Engram recall fallback. Kept
// dependency-free (no OpenCode/Bun APIs) so it's directly unit-testable;
// index.ts wires it to the `experimental.chat.system.transform` hook.
//
// This survives even if a compaction summary drops the methodology context:
// it is re-applied to the system prompt on every LLM call.
import type { CompactionIdentity } from "./compaction"

export function buildSystemRecall(identity: CompactionIdentity): string {
  return [
    "Recall Engram project methodology before acting.",
    `Project: ${identity.project} (family: ${identity.family}, line: ${identity.branchSuffix}).`,
    "Use Engram MCP (engram gateway) over CLI whenever possible for recall/retain/code-search.",
    "For Serena code navigation, prefer find_symbol for exact definitions and find_referencing_symbols for callers/references. Use search_for_pattern only for raw text/regex searches; keep regexes narrow and context limits small.",
  ].join("\n")
}
