// Pure helper for the Engram compaction hook. Kept dependency-free (no
// OpenCode/Bun APIs) so it's directly unit-testable; index.ts wires it to
// the `experimental.session.compacting` hook.
//
// The string returned here is pushed into `output.context`, so it survives
// `/compact` and auto-overflow summarization.

export interface CompactionIdentity {
  project: string
  family: string
  branchSuffix: string
}

export function buildCompactionContext(identity: CompactionIdentity): string {
  return [
    "## Engram Methodology Recall",
    "",
    `Project: ${identity.project} (family: ${identity.family}, line: ${identity.branchSuffix}).`,
    "Recall project methodology from Engram before continuing.",
    "Use Engram MCP (engram gateway) over CLI whenever possible for recall/retain/code-search.",
    "Preserve across compaction: active task + status, key decisions,",
    "files being modified, and next steps.",
  ].join("\n")
}
