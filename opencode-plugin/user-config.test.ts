import { describe, expect, test } from "bun:test"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { homedir, tmpdir } from "node:os"
import * as path from "node:path"
import {
  mergeEngramOptions,
  readEngramConfig,
  resolveEngramConfigPath,
  resolveEngramOptions,
  validateEngramOptions,
} from "./user-config"
import { PLUGIN_BUILD_INFO } from "./build-info"

describe("deployment-local plugin config", () => {
  test("missing config is optional", async () => {
    expect(await readEngramConfig(path.join(tmpdir(), "engram-config-does-not-exist.json"))).toEqual({})
  })

  test("loads identity and worktree policy from JSON", async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "engram-plugin-config-"))
    const configPath = path.join(directory, "opencode.json")
    try {
      await writeFile(configPath, JSON.stringify({
        gatewayUrl: "http://127.0.0.1:9999",
        repositories: { remotes: { "https://example.com/service-api": { project: "service-api", family: "platform" } } },
        worktreeIndex: { projects: ["service-api"], mcpUrl: "http://127.0.0.1:7999/mcp", embedding: "local/test-model" },
      }))

      await expect(readEngramConfig(configPath)).resolves.toEqual({
        gatewayUrl: "http://127.0.0.1:9999",
        repositories: { remotes: { "https://example.com/service-api": { project: "service-api", family: "platform" } } },
        worktreeIndex: { projects: ["service-api"], mcpUrl: "http://127.0.0.1:7999/mcp", embedding: "local/test-model" },
      })
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
  })

  test("rejects malformed root authorization instead of returning unsafe policy", async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "engram-plugin-config-invalid-"))
    const configPath = path.join(directory, "opencode.json")
    try {
      await writeFile(configPath, JSON.stringify({
        worktreeIndex: {
          familyRoots: { platform: ["relative/root"] },
        },
      }))

      await expect(readEngramConfig(configPath)).rejects.toThrow("absolute paths")
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
  })

  test("surfaces invalid deployment JSON instead of disabling policy", async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "engram-plugin-config-json-"))
    const configPath = path.join(directory, "opencode.json")
    try {
      await writeFile(configPath, "{not-json")
      await expect(readEngramConfig(configPath)).rejects.toThrow("invalid Engram plugin config")
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
  })

  test("rejects malformed top-level and nested identity options", () => {
    expect(() => validateEngramOptions({ gatewayUrl: 123 }, "explicit options")).toThrow("gatewayUrl")
    expect(() => validateEngramOptions({
      repositories: { remotes: { "https://example.com/service": { family: 123 } } },
    }, "explicit options")).toThrow("family")
  })

  test("validates the expected immutable plugin artifact", () => {
    expect(validateEngramOptions({
      pluginArtifact: {
        package: "engram-opencode-plugin",
        version: PLUGIN_BUILD_INFO.version,
        source: "npm",
      },
    }, "explicit options")).toEqual({
      pluginArtifact: {
        package: "engram-opencode-plugin",
        version: PLUGIN_BUILD_INFO.version,
        source: "npm",
      },
    })
    expect(() => validateEngramOptions({ pluginArtifact: { source: "npm" } }, "explicit options"))
      .toThrow("package or version")
    expect(() => validateEngramOptions({
      pluginArtifact: { package: "engram-opencode-plugin", version: PLUGIN_BUILD_INFO.version, source: "checkout" },
    }, "explicit options")).toThrow("source")
  })

  test("does not silently drop malformed explicit security policy", async () => {
    await expect(resolveEngramOptions({
      worktreeIndex: { allowedRoots: ["relative/root"] },
    })).rejects.toThrow("absolute paths")
  })

  test("explicit options override deployment config without dropping nested mappings", () => {
    const merged = mergeEngramOptions(
      {
        gatewayUrl: "http://configured",
        repositories: {
          directories: { "service-api": { family: "platform" } },
          remotes: { "https://example.com/service-api": { family: "platform" } },
        },
        worktreeIndex: {
          projects: ["service-api"],
          embedding: "local/configured",
          familyRoots: { platform: ["/repos/platform"], shared: ["/repos/shared"] },
        },
      },
      {
        gatewayUrl: "http://explicit",
        pluginArtifact: { package: "engram-opencode-plugin", version: PLUGIN_BUILD_INFO.version, source: "npm" },
        repositories: { remotes: { "https://example.com/service-ui": { family: "platform" } } },
        worktreeIndex: {
          mcpUrl: "http://127.0.0.1:7999/mcp",
          familyRoots: { platform: ["/repos/platform-override"] },
        },
      },
    )

    expect(merged.gatewayUrl).toBe("http://explicit")
    expect(merged.pluginArtifact).toEqual({
      package: "engram-opencode-plugin",
      version: PLUGIN_BUILD_INFO.version,
      source: "npm",
    })
    expect(merged.repositories?.directories?.["service-api"]).toEqual({ family: "platform" })
    expect(merged.repositories?.remotes?.["https://example.com/service-api"]).toEqual({ family: "platform" })
    expect(merged.repositories?.remotes?.["https://example.com/service-ui"]).toEqual({ family: "platform" })
    expect(merged.worktreeIndex).toEqual({
      projects: ["service-api"],
      embedding: "local/configured",
      mcpUrl: "http://127.0.0.1:7999/mcp",
      familyRoots: { platform: ["/repos/platform-override"], shared: ["/repos/shared"] },
    })
  })

  test("uses an explicit environment path when provided", () => {
    const previous = process.env["ENGRAM_OPENCODE_CONFIG"]
    process.env["ENGRAM_OPENCODE_CONFIG"] = "~/custom/opencode.json"
    try {
      expect(resolveEngramConfigPath()).toBe(path.join(process.env.HOME || homedir(), "custom/opencode.json"))
    } finally {
      if (previous === undefined) delete process.env["ENGRAM_OPENCODE_CONFIG"]
      else process.env["ENGRAM_OPENCODE_CONFIG"] = previous
    }
  })
})
