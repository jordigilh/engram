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

  test("release branch does not invent an unregistered gateway route", () => {
    const id = deriveIdentity({ directoryBasename: "kubernaut", branch: "release/v1.5", options: { family: "kubernaut" } })
    expect(id.project).toBe("kubernaut")
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

  test("explicit project selects an exact registered release route", () => {
    const id = deriveIdentity({ directoryBasename: "some-clone-dir", branch: "release/v1.5", options: { project: "kubernaut-v1.5", family: "kubernaut" } })
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
  test("produces one gateway entry", () => {
    const cfg = buildMcpConfig({ project: "kubernaut-v1.5", family: "kubernaut", branchSuffix: "v1.5" })
    expect(Object.keys(cfg)).toEqual(["engram"])
    expect(cfg.engram).toEqual({ type: "remote", url: "http://127.0.0.1:8896/mcp/kubernaut-v1.5", enabled: true })
  })

  test("builds an exact gateway route without using family to construct backend URLs", () => {
    const cfg = buildMcpConfig(
      { project: "kubernaut-v1.5", family: "kubernaut", branchSuffix: "v1.5" },
    )
    expect(cfg.engram).toEqual({ type: "remote", url: "http://127.0.0.1:8896/mcp/kubernaut-v1.5", enabled: true })
  })

  test("allows overriding only the gateway URL", () => {
    const cfg = buildMcpConfig(
      { project: "myrepo", family: "myrepo", branchSuffix: "main" },
      { gatewayUrl: "http://localhost:9999/" },
    )
    expect(cfg.engram.url).toBe("http://localhost:9999/mcp/myrepo")
  })

})
