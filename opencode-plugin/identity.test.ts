import { describe, expect, test } from "bun:test"
import {
  buildMcpConfig,
  deriveIdentity,
  normalizeRemote,
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

  test("directory routes override remote routes and top-level options", () => {
    const options = resolveRepositoryOptions({
      directory: "/workspace/service-api",
      remote: "git@github.com:acme/service-api.git",
      options: {
        project: "default-project",
        family: "default-family",
        repositories: {
          remotes: {
            "https://github.com/acme/service-api": {
              project: "remote-project",
              family: "remote-family",
            },
          },
          directories: {
            "/workspace/service-api": {
              project: "directory-project",
              family: "directory-family",
            },
          },
        },
      },
    })

    expect(options.project).toBe("directory-project")
    expect(options.family).toBe("directory-family")
  })

  test("remote route keys normalize SSH and HTTPS forms", () => {
    expect(normalizeRemote("git@github.com:Acme/Service-API.git")).toBe("https://github.com/acme/service-api")
    expect(normalizeRemote("https://github.com/acme/service-api/")).toBe("https://github.com/acme/service-api")
  })

  test("unmatched repository does not inherit another repository's branch routes", () => {
    const options = resolveRepositoryOptions({
      directory: "/workspace/service-api",
      remote: "https://github.com/acme/service-api",
      options: {
        repositories: {
          remotes: {
            "https://github.com/acme/other": {
              branchRoutes: { "release/v1.5": "other-v1.5" },
            },
          },
        },
      },
    })

    const id = deriveIdentity({ directoryBasename: "service-api", branch: "release/v1.5", options })
    expect(id.project).toBe("service-api")
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
