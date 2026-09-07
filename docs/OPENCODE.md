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

Release-line routes must be explicitly registered. For example, use
`kubernaut-v1.5` when that route is available rather than relying on the plugin
to invent a branch-suffixed URL.

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
