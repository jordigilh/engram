import { describe, expect, test } from "bun:test"
import * as path from "node:path"

async function readDeploymentConfig(name: string): Promise<Record<string, any>> {
  const file = path.join(import.meta.dir, "..", "deploy", name)
  return JSON.parse(await Bun.file(file).text()) as Record<string, any>
}

describe("OpenCode deployment templates", () => {
  test("production pins the npm package and rejects checkout sources", async () => {
    const config = await readDeploymentConfig("opencode-plugin.production.jsonc")
    const entry = config.plugins[0]
    expect(entry.package).toBe("engram-opencode-plugin@0.1.0")
    expect(entry.options.pluginArtifact).toEqual({
      package: "engram-opencode-plugin",
      version: "0.1.0",
      source: "npm",
    })
  })

  test("local development explicitly disables production and loads source", async () => {
    const config = await readDeploymentConfig("opencode-plugin.local.jsonc")
    expect(config.plugins[0]).toBe("-engram")
    expect(config.plugins[1].package).toBe("../opencode-plugin/index.ts")
    expect(config.plugins[1].options.pluginArtifact.source).toBe("local-checkout")
  })
})
