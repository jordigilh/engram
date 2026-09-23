import { describe, expect, test } from "bun:test"
import {
  deriveIdentity,
  buildMcpConfig,
  mergeMcpConfig,
  normalizeRepositoryRemote,
  resolveRepositoryOptions,
} from "./identity"

describe("deriveIdentity", () => {
  test("single repo, main branch, zero options: family and project both default to directory name, no branch suffix", () => {
    const id = deriveIdentity({ directoryBasename: "engram", branch: "main", options: {} })
    expect(id).toEqual({ project: "engram", family: "engram", branchSuffix: "main" })
  })

  test("family override lets N sibling repos share one bank while project stays per-repo", () => {
    const repoA = deriveIdentity({ directoryBasename: "service-api", branch: "main", options: { family: "platform" } })
    const repoB = deriveIdentity({ directoryBasename: "service-ui", branch: "main", options: { family: "platform" } })
    expect(repoA.family).toBe("platform")
    expect(repoB.family).toBe("platform")
    expect(repoA.project).not.toBe(repoB.project)
    expect(repoA.project).toBe("service-api")
    expect(repoB.project).toBe("service-ui")
  })

  test("release branch does not invent an unregistered gateway route", () => {
    const id = deriveIdentity({ directoryBasename: "service-api", branch: "release/v1.5", options: { family: "platform" } })
    expect(id.project).toBe("service-api")
    expect(id.family).toBe("platform")
    expect(id.branchSuffix).toBe("v1.5")
  })

  test("a feature/fix branch (not release/vX.Y) falls back to the main suffix", () => {
    const id = deriveIdentity({ directoryBasename: "service-api", branch: "feature/some-fix", options: {} })
    expect(id.branchSuffix).toBe("main")
    expect(id.project).toBe("service-api")
  })

  test("a dedicated per-release-line clone directory (name ends in -vX.Y) is detected without needing the branch at all", () => {
    const id = deriveIdentity({ directoryBasename: "service-api-v1.6", branch: "main", options: { family: "platform" } })
    expect(id.branchSuffix).toBe("v1.6")
    expect(id.project).toBe("service-api-v1.6")
    expect(id.family).toBe("platform")
  })

  test("explicit project selects an exact registered release route", () => {
    const id = deriveIdentity({ directoryBasename: "some-clone-dir", branch: "release/v1.5", options: { project: "service-api-v1.5", family: "platform" } })
    expect(id.project).toBe("service-api-v1.5")
    expect(id.family).toBe("platform")
  })

  test("configured branch route selects the release gateway mount", () => {
    const id = deriveIdentity({
      directoryBasename: "service-api",
      branch: "release/v1.5",
      options: {
        family: "platform",
        branchRoutes: { main: "service-api", "release/v1.5": "service-api-v1.5" },
      },
    })

    expect(id.project).toBe("service-api-v1.5")
    expect(id.family).toBe("platform")
  })

  test("explicit project takes precedence over configured branch route", () => {
    const id = deriveIdentity({
      directoryBasename: "service-api",
      branch: "release/v1.5",
      options: {
        project: "custom-route",
        branchRoutes: { "release/v1.5": "service-api-v1.5" },
      },
    })

    expect(id.project).toBe("custom-route")
  })

  test("git branch detection failing (e.g. not a git repo) falls back to main, not an error", () => {
    const id = deriveIdentity({ directoryBasename: "engram", branch: undefined, options: {} })
    expect(id.branchSuffix).toBe("main")
    expect(id.project).toBe("engram")
  })
})

describe("repository mappings", () => {
  test("normalizes SSH and .git remote forms", () => {
    expect(normalizeRepositoryRemote("git@github.com:dcm-project/dcm.git")).toBe("https://github.com/dcm-project/dcm")
    expect(normalizeRepositoryRemote("ssh://git@github.com/dcm-project/dcm.git")).toBe("https://github.com/dcm-project/dcm")
  })

  test("resolves an identity from a matching remote", () => {
    const options = resolveRepositoryOptions(
      "dcm",
      ["https://github.com/dcm-project/dcm.git"],
      { repositories: { remotes: { "https://github.com/dcm-project/dcm": { family: "dcm" } } } },
    )

    expect(options).toEqual({ family: "dcm" })
  })

  test("directory mappings take precedence for non-Git workspaces", () => {
    const options = resolveRepositoryOptions(
      "dcm-project",
      [],
      { repositories: { directories: { "dcm-project": { project: "dcm", family: "dcm" } } } },
    )

    expect(options).toEqual({ project: "dcm", family: "dcm" })
  })
})

describe("buildMcpConfig", () => {
  test("produces one gateway entry", () => {
    const cfg = buildMcpConfig({ project: "service-api-v1.5", family: "platform", branchSuffix: "v1.5" })
    expect(Object.keys(cfg)).toEqual(["engram"])
    expect(cfg.engram).toEqual({ type: "remote", url: "http://127.0.0.1:8896/mcp/service-api-v1.5", enabled: true })
  })

  test("builds an exact gateway route without using family to construct backend URLs", () => {
    const cfg = buildMcpConfig(
      { project: "service-api-v1.5", family: "platform", branchSuffix: "v1.5" },
    )
    expect(cfg.engram).toEqual({ type: "remote", url: "http://127.0.0.1:8896/mcp/service-api-v1.5", enabled: true })
  })

  test("allows overriding only the gateway URL", () => {
    const cfg = buildMcpConfig(
      { project: "myrepo", family: "myrepo", branchSuffix: "main" },
      { gatewayUrl: "http://localhost:9999/" },
    )
    expect(cfg.engram.url).toBe("http://localhost:9999/mcp/myrepo")
  })

})

describe("mergeMcpConfig", () => {
  test("preserves an explicit project-level Engram route", () => {
    const config: { mcp?: Record<string, unknown> } = {
      mcp: { engram: { type: "remote", url: "http://127.0.0.1:8896/mcp/dcm", enabled: true } },
    }
    const generated = buildMcpConfig({ project: "dcm-project", family: "dcm-project", branchSuffix: "main" })

    mergeMcpConfig(config, generated)

    expect(config.mcp?.engram).toEqual({
      type: "remote",
      url: "http://127.0.0.1:8896/mcp/dcm",
      enabled: true,
    })
  })

  test("adds the generated route when no explicit route exists", () => {
    const config: { mcp?: Record<string, unknown> } = {}
    const generated = buildMcpConfig({ project: "engram", family: "engram", branchSuffix: "main" })

    mergeMcpConfig(config, generated)

    expect(config.mcp?.engram).toEqual(generated.engram)
  })
})
