# OpenCode and OpenChamber Integration

Engram integrates with OpenCode and OpenChamber through one MCP entry. The
OpenCode plugin supplies project-aware recall, compaction context, and
MCP-over-CLI guidance. The gateway owns Hindsight, CocoIndex, Serena, and
optional project-specific backends.

The recommended setup is **hybrid**:

- Keep one explicit `mcp.engram` entry for the exact registered gateway route.
- Load the global Engram plugin for behavioral hooks and identity-aware
  methodology.
- The plugin adds its generated route only when no explicit `mcp.engram` entry
  exists; it never overwrites an explicit route.

## Configuration

Load the plugin globally from `opencode.jsonc` or configure it per repository.
The global configuration is preferred when OpenCode and OpenChamber share one
host:

```json
{
  "plugin": [
    [
      "/path/to/engram/opencode-plugin/index.ts",
      {
        "repositories": {
          "directories": {
            "<workspace-directory>": {
              "project": "<project-route>",
              "family": "<shared-family>"
            }
          },
          "remotes": {
            "https://github.com/<org>/<repo>": {
              "project": "<project-route>",
              "family": "<shared-family>"
            }
          }
        }
      }
    ]
  ],
  "mcp": {
    "engram": {
      "type": "remote",
      "url": "http://127.0.0.1:8896/mcp/<project-route>",
      "enabled": true
    }
  }
}
```

For a standalone repository whose directory name matches a registered gateway
route, the plugin can be used with zero repository mappings. Keep the explicit
gateway entry when the route is an alias, the checkout is an umbrella
directory, or the route differs from the directory name:

```json
{
  "plugin": ["/path/to/engram/opencode-plugin/index.ts"],
  "mcp": {
    "engram": {
      "type": "remote",
      "url": "http://127.0.0.1:8896/mcp/<project-route>",
      "enabled": true
    }
  }
}
```

The direct `mcp.engram` entry is the route authority. The plugin's generated
entry is only a fallback:

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

Users do not register Hindsight, CocoIndex, or Serena separately. Do not add a
second gateway entry through OpenChamber's Settings -> MCP; OpenChamber should
use the OpenCode server's resolved configuration.

## Project Identity

`project` is the exact gateway route and must match a registered project. The
gateway registry intentionally preserves different backend sets per project.
For example, one project may have docs and code backends while another also
has issues or Serena; the route's configured backend set is authoritative.

`family` identifies the shared docs/issues family. The gateway owns the actual
bank mapping; it is not used to construct direct backend URLs in the plugin.

Repository mappings are resolved in this order:

1. Explicit `project`, `family`, or `branchRoutes` plugin options.
2. A matching `directories` entry.
3. A matching normalized Git remote in `remotes`.
4. The checkout directory name and its base name.

SSH remotes and `.git` suffixes are normalized before matching. Use a
directory mapping for a non-Git workspace containing multiple repositories.

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

## Gateway

The gateway normally runs on `127.0.0.1:8896` under the
`io.vectorize.engram-runtime` launchd service. On macOS, the published runtime
image is the public front door; the native `io.vectorize.engram-gateway-native`
service listens privately on `127.0.0.1:8898` for host-only stdio backends. The
Hindsight API and backend daemons remain behind both gateway layers.

If OpenChamber is connected to a remote OpenCode/OpenChamber server,
`127.0.0.1` refers to that server. The gateway must be reachable from the
machine running the OpenCode server, not merely from the browser or mobile
client.

## Recall Output

The gateway normalizes Hindsight `recall` results before returning them to the
agent. Each response uses the `engram-recall.v1` structured schema and includes
the fact summary, provenance, tags, timestamps, compact scores, and an
explicit result count. Summaries use deterministic `key_sentences` metadata
when available rather than returning the source document chunk. It returns at
most eight records and bounds each summary; the `truncated` flag and text
notice identify when a query was too broad. Full Markdown remains available
through `get_mental_model`. Malformed or non-Hindsight tool output passes
through unchanged for diagnosis. Use a narrower query when the response
reports omitted results.

## Hybrid Examples

These are the intended patterns for the currently onboarded families:

| Workspace | Explicit gateway route | Plugin identity |
| --- | --- | --- |
| Kubernaut | `/mcp/kubernaut` | `family: kubernaut`; release branches map to registered aliases |
| Praxis | `/mcp/praxis` or the repository-specific `/mcp/praxis-*` route | `family: praxis`; repository mappings handle route-name exceptions |
| DCM | `/mcp/dcm` | `family: dcm`; the `dcm-project` umbrella directory maps to `project: dcm` |

The explicit route remains in each workspace's `opencode.json` or `.mcp.json`.
The global plugin supplies the hooks and uses the repository mapping to keep
the system-recall identity aligned with the route.

## Migration

Remove old direct MCP entries for `hindsight-docs`, `hindsight-issues`,
`cocoindex-code`, and `serena` after enabling the plugin. Keep one explicit
`mcp.engram` gateway entry when using the hybrid setup. Leaving the old backend
entries in place causes duplicate tools and bypasses the gateway's backend
isolation.

OpenChamber can inspect the resulting gateway entry through Settings -> MCP,
but the OpenCode configuration remains the source of truth for the route and
the plugin mappings.
