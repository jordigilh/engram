import { execFileSync } from "node:child_process"
import { mkdir, rm, writeFile } from "node:fs/promises"
import * as path from "node:path"

interface PackageManifest {
  name?: unknown
  version?: unknown
}

const packageDirectory = import.meta.dir

function gitRevision(): string {
  const configured = process.env["ENGRAM_PLUGIN_REVISION"]?.trim()
  if (configured) return configured
  try {
    const revision = execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: packageDirectory,
      encoding: "utf8",
    }).trim()
    if (!revision) return "unknown"
    const dirty = execFileSync("git", ["status", "--porcelain", "--untracked-files=all"], {
      cwd: packageDirectory,
      encoding: "utf8",
    }).trim()
    return dirty ? `${revision}-dirty` : revision
  } catch {
    return "unknown"
  }
}

const manifest = JSON.parse(await Bun.file(path.join(packageDirectory, "package.json")).text()) as PackageManifest
if (manifest.name !== "engram-opencode-plugin" || typeof manifest.version !== "string") {
  throw new Error("opencode-plugin/package.json must declare engram-opencode-plugin and a version")
}

const revision = gitRevision()
const buildInfo = {
  packageName: manifest.name,
  version: manifest.version,
  revision,
  source: "github-release" as const,
}

const outputDirectory = path.join(packageDirectory, "dist")
await rm(outputDirectory, { recursive: true, force: true })
await mkdir(outputDirectory, { recursive: true })

const result = await Bun.build({
  entrypoints: [path.join(packageDirectory, "index.ts")],
  outdir: outputDirectory,
  target: "bun",
  format: "esm",
  sourcemap: "external",
  define: {
    ENGRAM_PLUGIN_VERSION: JSON.stringify(manifest.version),
    ENGRAM_PLUGIN_REVISION: JSON.stringify(revision),
    ENGRAM_PLUGIN_SOURCE: JSON.stringify("github-release"),
  },
})

if (!result.success) {
  throw new Error("failed to build the OpenCode plugin")
}

await writeFile(path.join(outputDirectory, "build-info.json"), `${JSON.stringify(buildInfo, null, 2)}\n`, "utf8")
console.log(
  `Built ${buildInfo.packageName}@${buildInfo.version} ` +
  `revision=${buildInfo.revision} source=${buildInfo.source}`,
)
