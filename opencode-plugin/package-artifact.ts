import { execFileSync } from "node:child_process"
import { copyFile, mkdir, rm, writeFile } from "node:fs/promises"
import * as path from "node:path"

interface PackageManifest {
  name: string
  version: string
  description?: string
  license?: string
}

interface BuildInfo {
  packageName: string
  version: string
  revision: string
  source: string
}

const packageDirectory = import.meta.dir
const manifest = JSON.parse(await Bun.file(path.join(packageDirectory, "package.json")).text()) as PackageManifest
const buildInfo = JSON.parse(
  await Bun.file(path.join(packageDirectory, "dist", "build-info.json")).text(),
) as BuildInfo

if (manifest.name !== "engram-opencode-plugin" || buildInfo.source !== "github-release") {
  throw new Error("the release artifact must be built with github-release metadata")
}
if (manifest.version !== buildInfo.version || manifest.name !== buildInfo.packageName) {
  throw new Error("package.json and build-info.json do not describe the same artifact")
}

const releaseDirectory = path.join(packageDirectory, "release")
const artifactName = `${manifest.name}-${manifest.version}`
const artifactDirectory = path.join(releaseDirectory, artifactName)
const archivePath = path.join(releaseDirectory, `${artifactName}.tar.gz`)

await rm(releaseDirectory, { recursive: true, force: true })
await mkdir(artifactDirectory, { recursive: true })
await copyFile(path.join(packageDirectory, "dist", "index.js"), path.join(artifactDirectory, "index.js"))
await copyFile(path.join(packageDirectory, "dist", "build-info.json"), path.join(artifactDirectory, "build-info.json"))
await writeFile(
  path.join(artifactDirectory, "package.json"),
  `${JSON.stringify({
    name: manifest.name,
    version: manifest.version,
    description: manifest.description,
    license: manifest.license,
    type: "module",
    main: "index.js",
    exports: { ".": "./index.js" },
  }, null, 2)}\n`,
  "utf8",
)

execFileSync("tar", [
  "-czf",
  archivePath,
  "-C",
  artifactDirectory,
  "package.json",
  "index.js",
  "build-info.json",
])
const checksum = execFileSync("shasum", ["-a", "256", archivePath], { encoding: "utf8" }).trim().split(/\s+/)[0]
await writeFile(`${archivePath}.sha256`, `${checksum}  ${path.basename(archivePath)}\n`, "utf8")

console.log(`Built ${archivePath}`)
console.log(`SHA-256 ${checksum}`)
