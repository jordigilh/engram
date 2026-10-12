import { inspectGitRepository } from "./index.js"

const directory = process.argv[2] || process.cwd()
const result = inspectGitRepository(directory)
console.log(JSON.stringify({ directory, ...result }, null, 2))
