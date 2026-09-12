# OpenCode and OpenChamber Integration

Engram integrates with OpenCode and OpenChamber through one MCP entry. The
OpenCode plugin derives the current project identity and injects a single
remote MCP server pointing at `engram-gateway`. The gateway owns Hindsight,
CocoIndex, Serena, and optional project-specific backends.

## Configuration

Add the plugin to `opencode.json`, `opencode.jsonc`, or
`.opencode/opencode.json`:

```json
{
  "plugin": [
    [
      "/path/to/engram/opencode-plugin/index.ts",
      { "project": "kubernaut-operator", "family": "kubernaut" }
    ]
  ]
}
```

For a repository whose directory name matches a registered gateway route, the
plugin can be used with zero options:

```json
{
  "plugin": ["/path/to/engram/opencode-plugin/index.ts"]
}
```

The plugin injects one MCP server:

```json
{
  "mcp": {
    "engram": {
      "type": "remote",
      "url": "http://127.0.0.1:8896/mcp/kubernaut-operator",
      "enabled": true
    }
  }
}
```

Users do not register Hindsight, CocoIndex, or Serena separately.

## Project Identity

`project` is the exact gateway route and must match a registered project. The
gateway registry intentionally preserves different backend sets per project.
For example, `engram` has docs and code backends but no issues or Serena
backend.

`family` identifies the shared docs/issues family. The gateway owns the actual
bank mapping; it is not used to construct direct backend URLs in the plugin.

Release-line routes must be explicitly registered. Configure exact branch
routes when one checkout switches between release lines:

```json
{
  "plugin": [[
    "/path/to/engram/opencode-plugin/index.ts",
    {
      "family": "kubernaut",
      "branchRoutes": {
        "main": "kubernaut",
        "release/v1.5": "kubernaut-v1.5"
      }
    }
  ]]
}
```

For the current Kubernaut release policy, `kubernaut` is the main/current-v1.6
route and `kubernaut-v1.5` is the only separate release route until v1.6 is GA.
DCM and Praxis only require their main route; do not add a `*-v1.6` route.

Without `branchRoutes`, the plugin deliberately keeps the exact directory-name
route and never invents a route the gateway may not expose. Codex users can use
`scripts/engram-codex`, which applies the equivalent route as a per-invocation
`-c` override; see [Runtime Image](RUNTIME_IMAGE.md#branch-aware-codex).

## Gateway

The gateway normally runs on `127.0.0.1:8896` under the
`io.vectorize.engram-gateway` launchd service. The Hindsight API and backend
daemons remain behind it.

If OpenChamber is connected to a remote OpenCode/OpenChamber server,
`127.0.0.1` refers to that server. The gateway must be reachable from the
machine running the OpenCode server, not merely from the browser or mobile
client.

## Migration

Remove old direct MCP entries for `hindsight-docs`, `hindsight-issues`,
`cocoindex-code`, and `serena` after enabling the plugin. Leaving them in place
causes duplicate tools and bypasses the gateway's backend isolation.

OpenChamber can also create or inspect the resulting remote MCP entry through
Settings -> MCP, but the plugin configuration remains the source of truth for
automatic project identity.
