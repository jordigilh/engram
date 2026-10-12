import { describe, expect, test } from "bun:test"
import { mkdtemp, writeFile } from "node:fs/promises"
import { homedir, tmpdir } from "node:os"
import * as path from "node:path"
import {
  mergeEngramOptions,
  readEngramConfig,
  resolveEngramConfigPath,
} from "./user-config"

describe("deployment-local plugin config", () => {
  test("missing config is optional", async () => {
    expect(await readEngramConfig(path.join(tmpdir(), "engram-config-does-not-exist.json"))).toEqual({})
  })

  test("loads identity and worktree policy from JSON", async () => {
    const directory = await mkdtemp(path.join(tmpdir(), "engram-plugin-config-"))
    const configPath = path.join(directory, "opencode.json")
    await writeFile(configPath, JSON.stringify({
      gatewayUrl: "http://127.0.0.1:9999",
      repositories: { remotes: { "https://example.com/service-api": { project: "service-api", family: "platform" } } },
      worktreeIndex: { projects: ["service-api"], binary: "~/bin/zg", embedding: "local/test-model" },
    }))

    await expect(readEngramConfig(configPath)).resolves.toEqual({
      gatewayUrl: "http://127.0.0.1:9999",
      repositories: { remotes: { "https://example.com/service-api": { project: "service-api", family: "platform" } } },
      worktreeIndex: { projects: ["service-api"], binary: "~/bin/zg", embedding: "local/test-model" },
    })
  })

  test("explicit options override deployment config without dropping nested mappings", () => {
    const merged = mergeEngramOptions(
      {
        gatewayUrl: "http://configured",
        repositories: {
          directories: { "service-api": { family: "platform" } },
          remotes: { "https://example.com/service-api": { family: "platform" } },
        },
        worktreeIndex: { projects: ["service-api"], embedding: "local/configured" },
      },
      {
        gatewayUrl: "http://explicit",
        repositories: { remotes: { "https://example.com/service-ui": { family: "platform" } } },
        worktreeIndex: { binary: "/opt/zg" },
      },
    )

    expect(merged.gatewayUrl).toBe("http://explicit")
    expect(merged.repositories?.directories?.["service-api"]).toEqual({ family: "platform" })
    expect(merged.repositories?.remotes?.["https://example.com/service-api"]).toEqual({ family: "platform" })
    expect(merged.repositories?.remotes?.["https://example.com/service-ui"]).toEqual({ family: "platform" })
    expect(merged.worktreeIndex).toEqual({
      projects: ["service-api"],
      embedding: "local/configured",
      binary: "/opt/zg",
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
