import { describe, expect, test } from "bun:test"
import { buildCompactionContext } from "./compaction"
import { deriveIdentity } from "./identity"

describe("buildCompactionContext", () => {
  test("recalls methodology and prefers MCP over CLI", () => {
    const id = deriveIdentity({ directoryBasename: "engram", branch: "main", options: {} })
    const ctx = buildCompactionContext(id)
    expect(ctx).toMatch(/recall project methodology/i)
    expect(ctx).toMatch(/MCP.*over CLI|over CLI.*MCP/i)
    expect(ctx).toContain("engram")
  })

  test("carries project identity so the summary stays routed", () => {
    const ctx = buildCompactionContext({ project: "kubernaut-v1.5", family: "kubernaut", branchSuffix: "v1.5" })
    expect(ctx).toContain("kubernaut-v1.5")
    expect(ctx).toContain("kubernaut")
    expect(ctx).toContain("v1.5")
  })
})
