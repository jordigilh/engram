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
      { "project": "<project-route>", "family": "<shared-family>" }
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
      "url": "http://127.0.0.1:8896/mcp/<project-route>",
      "enabled": true
    }
  }
}
```

Users do not register Hindsight, CocoIndex, or Serena separately.

## Project Identity

`project` is the exact gateway route and must match a registered project. The
gateway registry intentionally preserves different backend sets per project.
For example, one project may have docs and code backends while another also
has issues or Serena; the route's configured backend set is authoritative.

`family` identifies the shared docs/issues family. The gateway owns the actual
bank mapping; it is not used to construct direct backend URLs in the plugin.

Release-line routes must be explicitly registered. Configure exact branch
routes when one checkout switches between release lines:

```json
{
  "plugin": [[
      "/path/to/engram/opencode-plugin/index.ts",
      {
        "family": "<shared-family>",
        "branchRoutes": {
          "main": "<project-route>",
          "release/vX.Y": "<project-route>-vX.Y"
        }
      }
  ]]
}
```

For a project with no separate release route, omit `branchRoutes`; the exact
directory-name route is used for every branch. Projects that publish separate
release routes should list those routes explicitly. The Kubernaut RCA release
policy is documented separately in [Kubernaut RCA MCP](KUBERNAUT_RCA_MCP.md).

Without `branchRoutes`, the plugin deliberately keeps the exact directory-name
route and never invents a route the gateway may not expose. Codex users can use
`scripts/engram-codex`, which applies the equivalent route as a per-invocation
`-c` override. Set `ENGRAM_CODEX_BRANCH_ROUTES` to a comma-separated mapping
such as `main=project,release/vX.Y=project-vX.Y`; see [Runtime Image](RUNTIME_IMAGE.md#branch-aware-codex).

### Global Repository Routing

A shared global plugin configuration can select repository-specific routes by
canonical directory or Git remote. Directory matches take precedence over
remote matches, and remote matches take precedence over the top-level options:

```json
{
  "plugin": [[
    "/path/to/engram/opencode-plugin/index.ts",
    {
      "project": "default-project",
      "repositories": {
        "remotes": {
          "https://github.com/acme/service-api": {
            "project": "service-api",
            "family": "platform",
            "branchRoutes": {
              "main": "service-api",
              "release/vX.Y": "service-api-vX.Y"
            }
          }
        },
        "directories": {
          "/workspaces/service-api": {
            "project": "service-api-local"
          }
        }
      }
    }
  ]]
}
```

Remote keys normalize HTTPS and SSH/scp forms, including a trailing `.git`.
Branch routes nested under a repository route are considered only after that
repository matches; they cannot leak into another checkout.

## Subagent Enforcement

OpenCode creates a subagent session with the current session's ID as
`parentID`. The plugin uses that persisted ancestry field rather than agent
names, so native and custom agents are handled identically. Root sessions have
no `parentID` and are not gated.

For child sessions, the plugin fails closed at both enforcement points:

1. `permission.ask` changes non-Engram requests to `deny`.
2. `tool.execute.before` throws before execution, including when a saved
   permission would otherwise bypass `permission.ask`.

Engram MCP tools are allowed before initialization. A child unlocks only when
`tool.execute.after` runs for an Engram tool, which means the MCP call returned
successfully. Denied or failed Engram calls do not unlock the session. Gate
state is isolated per session and removed when the session is deleted.

Forked sessions are not treated as subagents because OpenCode does not assign
them a `parentID`.

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
