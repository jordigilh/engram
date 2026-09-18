import { describe, expect, test } from "bun:test"
import { buildSystemRecall } from "./system-recall"
import { deriveIdentity } from "./identity"

describe("buildSystemRecall", () => {
  test("recalls methodology and prefers MCP over CLI on every prompt", () => {
    const id = deriveIdentity({ directoryBasename: "engram", branch: "main", options: {} })
    const text = buildSystemRecall(id)
    expect(text).toMatch(/recall.*methodology/i)
    expect(text).toMatch(/MCP.*over CLI|over CLI.*MCP/i)
    expect(text).toMatch(/prefer find_symbol.*find_referencing_symbols/i)
    expect(text).toMatch(/search_for_pattern only for raw text\/regex/i)
    expect(text).toContain("engram")
  })

  test("carries project identity", () => {
    const text = buildSystemRecall({ project: "kubernaut-v1.5", family: "kubernaut", branchSuffix: "v1.5" })
    expect(text).toContain("kubernaut-v1.5")
  })
})
