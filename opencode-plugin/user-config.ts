import { readFile } from "node:fs/promises"
import { homedir } from "node:os"
import * as path from "node:path"
import type { EngramPluginOptions, EngramRepositoryMappings, RepositoryIdentityOptions } from "./identity"
import type { WorktreeIndexPolicy } from "./worktree-index"

export const DEFAULT_ENGRAM_PLUGIN_CONFIG = ".engram/opencode.json"

export class EngramConfigValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "EngramConfigValidationError"
  }
}

type JsonObject = Record<string, unknown>

function isObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

function validateStringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string" && item.trim())) {
    throw new EngramConfigValidationError(`${label} must be an array of non-empty strings`)
  }
  return value.map((item) => item.trim())
}

function validateAbsoluteRoots(value: unknown, label: string): string[] {
  const roots = validateStringArray(value, label)
  if (!roots.every((root) => path.isAbsolute(root))) {
    throw new EngramConfigValidationError(`${label} must contain only absolute paths`)
  }
  return roots
}

function validateStringRecord(value: unknown, label: string): Record<string, string> {
  if (!isObject(value)) throw new EngramConfigValidationError(`${label} must be an object`)
  const result = Object.create(null) as Record<string, string>
  for (const [key, item] of Object.entries(value)) {
    if (!key.trim() || typeof item !== "string" || !item.trim()) {
      throw new EngramConfigValidationError(`${label} must map non-empty names to non-empty strings`)
    }
    result[key.trim()] = item.trim()
  }
  return result
}

function validateRepositoryIdentity(value: unknown, label: string): RepositoryIdentityOptions {
  if (!isObject(value)) throw new EngramConfigValidationError(`${label} must be an object`)
  const validated = { ...value } as RepositoryIdentityOptions
  for (const key of ["project", "family"] as const) {
    if (value[key] !== undefined && (typeof value[key] !== "string" || !value[key].trim())) {
      throw new EngramConfigValidationError(`${label}.${key} must be a non-empty string`)
    }
    if (typeof value[key] === "string") validated[key] = value[key].trim()
  }
  if (value.branchRoutes !== undefined) {
    validated.branchRoutes = validateStringRecord(value.branchRoutes, `${label}.branchRoutes`)
  }
  return validated
}

function validateRepositoryMappings(value: unknown, label: string): EngramRepositoryMappings {
  if (!isObject(value)) throw new EngramConfigValidationError(`${label} must be an object`)
  const validated = { ...value } as EngramRepositoryMappings
  for (const key of ["directories", "remotes"] as const) {
    if (value[key] === undefined) continue
    if (!isObject(value[key])) throw new EngramConfigValidationError(`${label}.${key} must be an object`)
    const mappings = Object.create(null) as Record<string, RepositoryIdentityOptions>
    for (const [name, options] of Object.entries(value[key])) {
      if (!name.trim()) throw new EngramConfigValidationError(`${label}.${key} contains an empty repository name`)
      mappings[name.trim()] = validateRepositoryIdentity(options, `${label}.${key}.${name}`)
    }
    validated[key] = mappings
  }
  return validated
}

/** Validate the untyped JSON policy before it reaches path/indexer code. */
export function validateWorktreeIndexPolicy(
  value: unknown,
  label = "worktreeIndex",
): WorktreeIndexPolicy | undefined {
  if (value === undefined) return undefined
  if (!isObject(value)) throw new EngramConfigValidationError(`${label} must be an object`)

  const validated = { ...value } as WorktreeIndexPolicy

  if (value.enabled !== undefined && typeof value.enabled !== "boolean") {
    throw new EngramConfigValidationError(`${label}.enabled must be a boolean`)
  }
  if (value.gateSessions !== undefined && typeof value.gateSessions !== "boolean") {
    throw new EngramConfigValidationError(`${label}.gateSessions must be a boolean`)
  }
  if (value.projects !== undefined) {
    validated.projects = validateStringArray(value.projects, `${label}.projects`)
  }
  for (const key of ["mcpUrl", "embedding"] as const) {
    if (value[key] !== undefined && (typeof value[key] !== "string" || !value[key].trim())) {
      throw new EngramConfigValidationError(`${label}.${key} must be a non-empty string`)
    }
    if (typeof value[key] === "string") validated[key] = value[key].trim()
  }
  for (const key of ["allowedRoots", "aggregateRoots"] as const) {
    if (value[key] !== undefined) validated[key] = validateAbsoluteRoots(value[key], `${label}.${key}`)
  }

  if (value.familyRoots !== undefined) {
    if (!isObject(value.familyRoots)) throw new EngramConfigValidationError(`${label}.familyRoots must be an object`)
    const familyRoots = Object.create(null) as Record<string, string[]>
    for (const [family, roots] of Object.entries(value.familyRoots)) {
      if (!family.trim()) throw new EngramConfigValidationError(`${label}.familyRoots contains an empty family name`)
      const normalizedFamily = family.trim()
      if (Object.prototype.hasOwnProperty.call(familyRoots, normalizedFamily)) {
        throw new EngramConfigValidationError(`${label}.familyRoots contains duplicate family name ${normalizedFamily}`)
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

export function validateEngramOptions(value: unknown, label: string): Partial<EngramPluginOptions> {
  if (!isObject(value)) throw new EngramConfigValidationError(`${label} must be an object`)
  const validated = { ...value } as Partial<EngramPluginOptions>
  for (const key of ["project", "family", "gatewayUrl"] as const) {
    if (value[key] !== undefined && (typeof value[key] !== "string" || !value[key].trim())) {
      throw new EngramConfigValidationError(`${label}.${key} must be a non-empty string`)
    }
    if (typeof value[key] === "string") validated[key] = value[key].trim()
  }
  if (value.branchRoutes !== undefined) {
    validated.branchRoutes = validateStringRecord(value.branchRoutes, `${label}.branchRoutes`)
  }
  if (value.repositories !== undefined) {
    validated.repositories = validateRepositoryMappings(value.repositories, `${label}.repositories`)
  }
  const worktreeIndex = validateWorktreeIndexPolicy(value.worktreeIndex, `${label}.worktreeIndex`)
  if (worktreeIndex !== undefined) validated.worktreeIndex = worktreeIndex
  return validated
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
  const merged = Object.create(null) as T
  Object.assign(merged, base || {}, override || {})
  return merged
}

function mergeWorktreeIndex(
  configured: WorktreeIndexPolicy | undefined,
  explicit: WorktreeIndexPolicy | undefined,
): WorktreeIndexPolicy | undefined {
  if (!configured && !explicit) return undefined
  const merged = { ...(configured || {}), ...(explicit || {}) }
  if (configured?.familyRoots || explicit?.familyRoots) {
    const familyRoots = Object.create(null) as Record<string, string[]>
    Object.assign(familyRoots, configured?.familyRoots || {}, explicit?.familyRoots || {})
    merged.familyRoots = familyRoots
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

/** Load deployment config; a missing file is optional, but invalid policy is fatal. */
export async function resolveEngramOptions(rawOptions: unknown): Promise<EngramPluginOptions> {
  const explicit = rawOptions === undefined
    ? {}
    : validateEngramOptions(rawOptions, "explicit plugin options")
  const configured = await readEngramConfig()
  return mergeEngramOptions(configured, explicit)
}
