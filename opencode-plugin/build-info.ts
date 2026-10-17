import { readFile } from "node:fs/promises"
import * as path from "node:path"
import { fileURLToPath } from "node:url"
import packageManifest from "./package.json" with { type: "json" }

/** Build metadata embedded in the release artifact and reported at startup. */
export type PluginArtifactSource = "github-release" | "local-checkout"

export interface PluginBuildInfo {
  packageName: string
  version: string
  revision: string
  source: PluginArtifactSource
}

export interface PluginArtifactExpectation {
  /** Expected package name, normally `engram-opencode-plugin`. */
  package?: string
  /** Exact semver that OpenCode should have loaded. */
  version?: string
  /** Optional source revision embedded by the package build. */
  revision?: string
  /** `github-release` rejects a mutable local checkout in production. */
  source?: PluginArtifactSource
}

export class EngramPluginArtifactValidationError extends Error {
  readonly expected: PluginArtifactExpectation
  readonly actual: PluginBuildInfo

  constructor(expected: PluginArtifactExpectation, actual: PluginBuildInfo, reason: string) {
    super(
      `[engram-plugin] artifact validation failed: ${reason}; ` +
      `expected package=${expected.package || "*"} version=${expected.version || "*"} ` +
      `revision=${expected.revision || "*"} source=${expected.source || "*"}; ` +
      `loaded package=${actual.packageName} version=${actual.version} ` +
      `revision=${actual.revision} source=${actual.source}`,
    )
    this.name = "EngramPluginArtifactValidationError"
    this.expected = expected
    this.actual = actual
  }
}

const PACKAGE_NAME = "engram-opencode-plugin"
const SOURCE_VERSION = typeof packageManifest.version === "string" ? packageManifest.version : "unknown"

// Bun replaces these identifiers during the package build. `typeof` keeps the
// source checkout usable without a generated file or runtime build step.
declare const ENGRAM_PLUGIN_VERSION: string
declare const ENGRAM_PLUGIN_REVISION: string
declare const ENGRAM_PLUGIN_SOURCE: PluginArtifactSource

function compiledValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback
}

export const PLUGIN_BUILD_INFO: PluginBuildInfo = Object.freeze({
  packageName: PACKAGE_NAME,
  version: compiledValue(
    typeof ENGRAM_PLUGIN_VERSION === "string" ? ENGRAM_PLUGIN_VERSION : undefined,
    SOURCE_VERSION,
  ),
  revision: compiledValue(
    typeof ENGRAM_PLUGIN_REVISION === "string" ? ENGRAM_PLUGIN_REVISION : undefined,
    "development",
  ),
  source: (typeof ENGRAM_PLUGIN_SOURCE === "string" ? ENGRAM_PLUGIN_SOURCE : "local-checkout") as PluginArtifactSource,
})

export function validatePluginArtifact(
  expected: PluginArtifactExpectation | undefined,
  actual: PluginBuildInfo = PLUGIN_BUILD_INFO,
): void {
  if (!expected) return
  if (!expected.package && !expected.version) {
    throw new EngramPluginArtifactValidationError(expected, actual, "package and version are required")
  }
  if (expected.package && expected.package !== actual.packageName) {
    throw new EngramPluginArtifactValidationError(expected, actual, "package name mismatch")
  }
  if (expected.version && expected.version !== actual.version) {
    throw new EngramPluginArtifactValidationError(expected, actual, "version mismatch")
  }
  if (expected.revision && expected.revision !== actual.revision) {
    throw new EngramPluginArtifactValidationError(expected, actual, "revision mismatch")
  }
  if (expected.source && expected.source !== actual.source) {
    throw new EngramPluginArtifactValidationError(expected, actual, "source mismatch")
  }
}

/**
 * Confirm that the loaded module still has the package manifest that produced
 * it. OpenCode's npm loader guarantees this for a package install, while a
 * local source override resolves the manifest beside index.ts.
 */
export async function validatePluginArtifactManifest(
  expected: PluginArtifactExpectation | undefined,
  actual: PluginBuildInfo = PLUGIN_BUILD_INFO,
  moduleURL = import.meta.url,
): Promise<void> {
  validatePluginArtifact(expected, actual)

  const moduleDirectory = path.dirname(fileURLToPath(moduleURL))
  const candidates = [
    path.join(moduleDirectory, "package.json"),
    path.join(moduleDirectory, "..", "package.json"),
  ]
  let manifestPath: string | undefined
  let raw: string | undefined
  for (const candidate of candidates) {
    try {
      raw = await readFile(candidate, "utf8")
      manifestPath = candidate
      break
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
    }
  }

  const fallbackExpected = expected || { package: actual.packageName, version: actual.version }
  if (!raw || !manifestPath) {
    throw new EngramPluginArtifactValidationError(fallbackExpected, actual, "package manifest not found")
  }

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    throw new EngramPluginArtifactValidationError(
      fallbackExpected,
      actual,
      `invalid package manifest at ${manifestPath}`,
    )
  }
  const manifest = parsed as { name?: unknown; version?: unknown }
  if (manifest.name !== actual.packageName || manifest.version !== actual.version) {
    throw new EngramPluginArtifactValidationError(
      fallbackExpected,
      actual,
      `package manifest mismatch at ${manifestPath}`,
    )
  }
}
