import { describe, expect, test } from "bun:test"
import { deriveIdentity, buildMcpConfig } from "./identity"

describe("deriveIdentity", () => {
  test("single repo, main branch, zero options: family and project both default to directory name, no branch suffix", () => {
    const id = deriveIdentity({ directoryBasename: "engram", branch: "main", options: {} })
    expect(id).toEqual({ project: "engram", family: "engram", branchSuffix: "main" })
  })

  test("family override lets N sibling repos share one bank while project stays per-repo", () => {
    const repoA = deriveIdentity({ directoryBasename: "kubernaut-operator", branch: "main", options: { family: "kubernaut" } })
    const repoB = deriveIdentity({ directoryBasename: "kubernaut-console", branch: "main", options: { family: "kubernaut" } })
    expect(repoA.family).toBe("kubernaut")
    expect(repoB.family).toBe("kubernaut")
    expect(repoA.project).not.toBe(repoB.project)
    expect(repoA.project).toBe("kubernaut-operator")
    expect(repoB.project).toBe("kubernaut-console")
  })

  test("checked-out release/vX.Y branch suffixes project but never family", () => {
    const id = deriveIdentity({ directoryBasename: "kubernaut", branch: "release/v1.5", options: { family: "kubernaut" } })
    expect(id.project).toBe("kubernaut-v1.5")
    expect(id.family).toBe("kubernaut")
    expect(id.branchSuffix).toBe("v1.5")
  })

  test("a feature/fix branch (not release/vX.Y) falls back to the main suffix", () => {
    const id = deriveIdentity({ directoryBasename: "kubernaut", branch: "feature/some-fix", options: {} })
    expect(id.branchSuffix).toBe("main")
    expect(id.project).toBe("kubernaut")
  })

  test("a dedicated per-release-line clone directory (name ends in -vX.Y) is detected without needing the branch at all", () => {
    const id = deriveIdentity({ directoryBasename: "kubernaut-v1.6", branch: "main", options: { family: "kubernaut" } })
    expect(id.branchSuffix).toBe("v1.6")
    expect(id.project).toBe("kubernaut-v1.6")
    expect(id.family).toBe("kubernaut")
  })

  test("explicit project override still gets branch-suffixed on a release branch", () => {
    const id = deriveIdentity({ directoryBasename: "some-clone-dir", branch: "release/v1.5", options: { project: "kubernaut" } })
    expect(id.project).toBe("kubernaut-v1.5")
    expect(id.family).toBe("kubernaut")
  })

  test("git branch detection failing (e.g. not a git repo) falls back to main, not an error", () => {
    const id = deriveIdentity({ directoryBasename: "engram", branch: undefined, options: {} })
    expect(id.branchSuffix).toBe("main")
    expect(id.project).toBe("engram")
  })
})

describe("buildMcpConfig", () => {
  test("produces the 4 backend entries using the production URL conventions", () => {
    const cfg = buildMcpConfig({ project: "kubernaut-v1.5", family: "kubernaut", branchSuffix: "v1.5" })
    expect(cfg["hindsight-docs"]).toEqual({ type: "remote", url: "http://localhost:8888/mcp/kubernaut-docs/", enabled: true })
    expect(cfg["hindsight-issues"]).toEqual({ type: "remote", url: "http://localhost:8888/mcp/kubernaut-issues/", enabled: true })
    expect(cfg["cocoindex-code"]).toEqual({ type: "remote", url: "http://127.0.0.1:8891/mcp", enabled: true })
    expect(cfg["serena"]).toEqual({ type: "remote", url: "http://127.0.0.1:8893/mcp/kubernaut-v1.5", enabled: true })
  })

  test("family bank URLs stay identical across two different projects sharing one family", () => {
    const a = buildMcpConfig({ project: "kubernaut-operator", family: "kubernaut", branchSuffix: "main" })
    const b = buildMcpConfig({ project: "kubernaut-console", family: "kubernaut", branchSuffix: "main" })
    expect(a["hindsight-docs"].url).toBe(b["hindsight-docs"].url)
    expect(a["hindsight-issues"].url).toBe(b["hindsight-issues"].url)
    expect(a["serena"].url).not.toBe(b["serena"].url)
  })

  test("allows overriding the hindsight base URL and ports for non-default deployments", () => {
    const cfg = buildMcpConfig(
      { project: "myrepo", family: "myrepo", branchSuffix: "main" },
      { hindsightBaseUrl: "http://localhost:9999", cocoindexUrl: "http://127.0.0.1:9001/mcp", serenaMultiplexUrl: "http://127.0.0.1:9003" },
    )
    expect(cfg["hindsight-docs"].url).toBe("http://localhost:9999/mcp/myrepo-docs/")
    expect(cfg["cocoindex-code"].url).toBe("http://127.0.0.1:9001/mcp")
    expect(cfg["serena"].url).toBe("http://127.0.0.1:9003/mcp/myrepo")
  })
})
