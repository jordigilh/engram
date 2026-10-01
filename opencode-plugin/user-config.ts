import { readFile } from "node:fs/promises"
import { homedir } from "node:os"
import * as path from "node:path"
import type { EngramPluginOptions } from "./identity"

export const DEFAULT_ENGRAM_PLUGIN_CONFIG = ".engram/opencode.json"

type JsonObject = Record<string, unknown>

function isObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
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
  return parsed as Partial<EngramPluginOptions>
}

function mergeRecord<T extends JsonObject>(base: T | undefined, override: T | undefined): T | undefined {
  if (!base && !override) return undefined
  return { ...(base || {}), ...(override || {}) } as T
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
  merged.worktreeIndex = mergeRecord(configured.worktreeIndex, explicit.worktreeIndex)
  return merged
}

/** Load deployment config without making a malformed optional file fatal to a session. */
export async function resolveEngramOptions(rawOptions: unknown): Promise<EngramPluginOptions> {
  const explicit = isObject(rawOptions) ? (rawOptions as Partial<EngramPluginOptions>) : {}
  let configured: Partial<EngramPluginOptions> = {}
  try {
    configured = await readEngramConfig()
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    console.error(`[engram-plugin] ${message}`)
  }
  return mergeEngramOptions(configured, explicit)
}
