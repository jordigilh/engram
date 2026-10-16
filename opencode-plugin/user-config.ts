import { readFile } from "node:fs/promises"
import { homedir } from "node:os"
import * as path from "node:path"
import type { EngramPluginOptions } from "./identity"
import type { WorktreeIndexPolicy } from "./worktree-index"

export const DEFAULT_ENGRAM_PLUGIN_CONFIG = ".engram/opencode.json"

type JsonObject = Record<string, unknown>

function isObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

function validateStringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string" && item.trim())) {
    throw new Error(`${label} must be an array of non-empty strings`)
  }
  return value.map((item) => item.trim())
}

function validateAbsoluteRoots(value: unknown, label: string): string[] {
  const roots = validateStringArray(value, label)
  if (!roots.every((root) => path.isAbsolute(root))) {
    throw new Error(`${label} must contain only absolute paths`)
  }
  return roots
}

/** Validate the untyped JSON policy before it reaches path/indexer code. */
export function validateWorktreeIndexPolicy(
  value: unknown,
  label = "worktreeIndex",
): WorktreeIndexPolicy | undefined {
  if (value === undefined) return undefined
  if (!isObject(value)) throw new Error(`${label} must be an object`)

  const validated = { ...value } as WorktreeIndexPolicy

  if (value.enabled !== undefined && typeof value.enabled !== "boolean") {
    throw new Error(`${label}.enabled must be a boolean`)
  }
  if (value.gateSessions !== undefined && typeof value.gateSessions !== "boolean") {
    throw new Error(`${label}.gateSessions must be a boolean`)
  }
  if (value.projects !== undefined) {
    validated.projects = validateStringArray(value.projects, `${label}.projects`)
  }
  for (const key of ["mcpUrl", "embedding"] as const) {
    if (value[key] !== undefined && (typeof value[key] !== "string" || !value[key].trim())) {
      throw new Error(`${label}.${key} must be a non-empty string`)
    }
    if (typeof value[key] === "string") validated[key] = value[key].trim()
  }
  for (const key of ["allowedRoots", "aggregateRoots"] as const) {
    if (value[key] !== undefined) validated[key] = validateAbsoluteRoots(value[key], `${label}.${key}`)
  }

  if (value.familyRoots !== undefined) {
    if (!isObject(value.familyRoots)) throw new Error(`${label}.familyRoots must be an object`)
    const familyRoots = Object.create(null) as Record<string, string[]>
    for (const [family, roots] of Object.entries(value.familyRoots)) {
      if (!family.trim()) throw new Error(`${label}.familyRoots contains an empty family name`)
      const normalizedFamily = family.trim()
      if (Object.prototype.hasOwnProperty.call(familyRoots, normalizedFamily)) {
        throw new Error(`${label}.familyRoots contains duplicate family name ${normalizedFamily}`)
      }
      familyRoots[normalizedFamily] = validateAbsoluteRoots(
        roots,
        `${label}.familyRoots.${family}`,
      )
    }
    validated.familyRoots = familyRoots
  }

  return validated
}

function validateEngramOptions(value: unknown, label: string): Partial<EngramPluginOptions> {
  if (!isObject(value)) throw new Error(`${label} must be an object`)
  const worktreeIndex = validateWorktreeIndexPolicy(value.worktreeIndex, `${label}.worktreeIndex`)
  return worktreeIndex === undefined
    ? value as Partial<EngramPluginOptions>
    : { ...value, worktreeIndex } as Partial<EngramPluginOptions>
}

/** Resolve the deployment-local plugin configuration path. */
export function resolveEngramConfigPath(): string {
  const configured = process.env["ENGRAM_OPENCODE_CONFIG"]?.trim()
  if (configured) return path.resolve(configured.replace(/^~(?=\/|$)/, homedir()))
  return path.join(process.env["HOME"] || homedir(), DEFAULT_ENGRAM_PLUGIN_CONFIG)
}

/** Read the optional deployment-local plugin configuration. */
export async function readEngramConfig(configPath = resolveEngramConfigPath()): Promise<Partial<EngramPluginOptions>> {
  let raw: string
  try {
    raw = await readFile(configPath, "utf8")
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {}
    throw error
  }

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    throw new Error(`invalid Engram plugin config ${configPath}: ${message}`)
  }
  if (!isObject(parsed)) throw new Error(`Engram plugin config ${configPath} must contain a JSON object`)
  return validateEngramOptions(parsed, `Engram plugin config ${configPath}`)
}

function mergeRecord<T extends JsonObject>(base: T | undefined, override: T | undefined): T | undefined {
  if (!base && !override) return undefined
  return { ...(base || {}), ...(override || {}) } as T
}

function mergeWorktreeIndex(
  configured: WorktreeIndexPolicy | undefined,
  explicit: WorktreeIndexPolicy | undefined,
): WorktreeIndexPolicy | undefined {
  if (!configured && !explicit) return undefined
  const merged = { ...(configured || {}), ...(explicit || {}) }
  if (configured?.familyRoots || explicit?.familyRoots) {
    merged.familyRoots = {
      ...(configured?.familyRoots || {}),
      ...(explicit?.familyRoots || {}),
    }
  }
  return merged
}

/** Merge deployment config first, with explicit OpenCode options as an override. */
export function mergeEngramOptions(
  configured: Partial<EngramPluginOptions>,
  explicit: Partial<EngramPluginOptions>,
): EngramPluginOptions {
  const merged: EngramPluginOptions = { ...configured, ...explicit }
  merged.branchRoutes = mergeRecord(configured.branchRoutes, explicit.branchRoutes)
  merged.repositories = mergeRecord(configured.repositories, explicit.repositories)
  if (configured.repositories || explicit.repositories) {
    merged.repositories = {
      ...merged.repositories,
      directories: mergeRecord(configured.repositories?.directories, explicit.repositories?.directories),
      remotes: mergeRecord(configured.repositories?.remotes, explicit.repositories?.remotes),
    }
  }
  merged.worktreeIndex = mergeWorktreeIndex(configured.worktreeIndex, explicit.worktreeIndex)
  return merged
}

/** Load deployment config without making a malformed optional file fatal to a session. */
export async function resolveEngramOptions(rawOptions: unknown): Promise<EngramPluginOptions> {
  let explicit: Partial<EngramPluginOptions> = {}
  if (isObject(rawOptions)) {
    try {
      explicit = validateEngramOptions(rawOptions, "explicit plugin options")
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      console.error(`[engram-plugin] ${message}`)
    }
  }
  let configured: Partial<EngramPluginOptions> = {}
  try {
    configured = await readEngramConfig()
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    console.error(`[engram-plugin] ${message}`)
  }
  return mergeEngramOptions(configured, explicit)
}
