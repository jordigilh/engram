import { describe, expect, test } from "bun:test"
import {
  EngramPluginArtifactValidationError,
  PLUGIN_BUILD_INFO,
  validatePluginArtifact,
  validatePluginArtifactManifest,
} from "./build-info"
import packageManifest from "./package.json" with { type: "json" }

const PACKAGE_VERSION = packageManifest.version

describe("plugin build metadata", () => {
  test("identifies a source checkout without pretending it is a release", () => {
    expect(PLUGIN_BUILD_INFO).toEqual({
      packageName: "engram-opencode-plugin",
      version: PACKAGE_VERSION,
      revision: "development",
      source: "local-checkout",
    })
  })

  test("accepts the expected local development artifact", () => {
    expect(() => validatePluginArtifact({
      package: "engram-opencode-plugin",
      version: PACKAGE_VERSION,
      revision: "development",
      source: "local-checkout",
    })).not.toThrow()
  })

  test("rejects an artifact with a different version", () => {
    expect(() => validatePluginArtifact({
      package: "engram-opencode-plugin",
      version: "0.2.0",
    })).toThrow("version mismatch")
  })

  test("rejects a local checkout when production requires npm", () => {
    let error: unknown
    try {
      validatePluginArtifact({
        package: "engram-opencode-plugin",
        version: PACKAGE_VERSION,
        source: "npm",
      })
    } catch (caught) {
      error = caught
    }
    expect(error).toBeInstanceOf(EngramPluginArtifactValidationError)
  })

  test("rejects an incomplete expectation", () => {
    expect(() => validatePluginArtifact({ source: "npm" })).toThrow("package and version are required")
  })

  test("validates an embedded revision when the deployment pins one", () => {
    expect(() => validatePluginArtifact(
      { package: "engram-opencode-plugin", version: PACKAGE_VERSION, revision: "abc123" },
      { packageName: "engram-opencode-plugin", version: PACKAGE_VERSION, revision: "abc123", source: "npm" },
    )).not.toThrow()

    expect(() => validatePluginArtifact(
      { package: "engram-opencode-plugin", version: PACKAGE_VERSION, revision: "wrong" },
      { packageName: "engram-opencode-plugin", version: PACKAGE_VERSION, revision: "abc123", source: "npm" },
    )).toThrow("revision mismatch")
  })

  test("confirms the source checkout manifest exists and matches", async () => {
    await expect(validatePluginArtifactManifest({
      package: "engram-opencode-plugin",
      version: PACKAGE_VERSION,
      revision: "development",
      source: "local-checkout",
    })).resolves.toBeUndefined()
  })

  test("rejects a module URL whose package manifest is missing", async () => {
    await expect(validatePluginArtifactManifest(
      { package: "engram-opencode-plugin", version: PACKAGE_VERSION },
      PLUGIN_BUILD_INFO,
      "file:///definitely/not/a/plugin/index.js",
    )).rejects.toBeInstanceOf(EngramPluginArtifactValidationError)
  })
})
