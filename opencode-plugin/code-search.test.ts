import { describe, expect, test } from "bun:test"
import { detectCodeSearch, buildCodeSearchNudge } from "./code-search"

describe("detectCodeSearch", () => {
  test("matches read-only search CLIs via bash", () => {
    expect(detectCodeSearch("bash", { command: "grep -r foo src" })?.cli).toBe("grep")
    expect(detectCodeSearch("bash", { command: "rg 'my query' src" })?.query).toBe("my query")
    expect(detectCodeSearch("bash", { command: "git grep TODO" })?.cli).toBe("git grep")
    expect(detectCodeSearch("bash", { command: "awk '/pattern/ {print $1}' f.log" })?.cli).toBe("awk")
    expect(detectCodeSearch("bash", { command: "sed -n '1,80p' file.ts" })?.cli).toBe("sed -n")
    expect(detectCodeSearch("bash", { command: "find . -name '*.ts'" })?.cli).toBe("find")
  })

  test("matches built-in grep/glob tools", () => {
    expect(detectCodeSearch("grep", { pattern: "TODO" })?.query).toBe("TODO")
    expect(detectCodeSearch("glob", { pattern: "**/*.ts" })?.cli).toBe("glob")
  })

  test("never matches replace operations or engram tools", () => {
    expect(detectCodeSearch("bash", { command: "sed -i 's/a/b/' f" })).toBeNull()
    expect(detectCodeSearch("bash", { command: "perl -pi -e 's/a/b/' f" })).toBeNull()
    expect(detectCodeSearch("edit", { filePath: "f" })).toBeNull()
    expect(detectCodeSearch("write", { filePath: "f" })).toBeNull()
    expect(detectCodeSearch("apply_patch", { patchText: "x" })).toBeNull()
    expect(detectCodeSearch("bash", { command: "echo hello" })).toBeNull()
    expect(detectCodeSearch("engram_recall", { query: "x" })).toBeNull()
    expect(detectCodeSearch("bash", { command: "awk -i inplace '{print}' f" })).toBeNull()
  })

  test("nudge is engram-only", () => {
    const msg = buildCodeSearchNudge({ cli: "grep", query: "TODO" })
    expect(msg).toMatch(/engram/i)
    expect(msg).not.toMatch(/hindsight|cocoindex|serena/i)
  })
})
