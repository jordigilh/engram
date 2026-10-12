import { tmpdir } from "node:os"
import * as path from "node:path"

// Plugin setup tests must not read the developer's deployment-local policy.
// Production uses the default ~/.engram/opencode.json path.
process.env["ENGRAM_OPENCODE_CONFIG"] = path.join(tmpdir(), "engram-opencode-plugin-test-config.json")
