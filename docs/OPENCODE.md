# OpenCode and OpenChamber Integration

Engram integrates with OpenCode and OpenChamber through one MCP entry. The
OpenCode plugin supplies project-aware recall, compaction context, and
MCP-over-CLI guidance. The gateway owns Hindsight, CocoIndex, Serena, and
optional project-specific backends.

The recommended setup is **hybrid**:

- Keep one explicit `mcp.servers.engram` entry for the exact registered
  gateway route.
- Load the global Engram plugin for behavioral hooks and identity-aware
  methodology.
- The plugin adds its generated route only when no explicit
  `mcp.servers.engram` entry exists; it never overwrites an explicit route.

## Configuration

> **OpenCode V2:** V2 does not run V1 plugin implementations. Engram now uses
> the V2 `Plugin.define` API to register MCP configuration, session hooks, tool
> hooks, and its event subscription. Configure it with the native `plugins`
> shape below. Point the plugin entry at the **directory**
> (`.../opencode-plugin`), not `index.ts`; OpenCode V2 requires a directory for
> this local plugin.

Load the plugin globally from `opencode.jsonc` or configure it per repository.
The global configuration is preferred when OpenCode and OpenChamber share one
host. Native V2 form:

Keep the deployment-specific gateway, repository, branch, and worktree policy
in `~/.engram/opencode.json`; the inline `options` object below is useful for a
temporary override or an isolated test.

```json
{
  "plugins": [
    {
      "package": "/path/to/engram/opencode-plugin",
      "options": {
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
    }
  ],
  "mcp": {
    "servers": {
      "engram": {
        "type": "remote",
        "url": "http://127.0.0.1:8896/mcp/<project-route>",
        "oauth": false,
        "disabled": false
      }
    }
  }
}
```

`oauth: false` is required: V2 attempts OAuth discovery on remote servers by
default, and the gateway is a plain-LAN endpoint with no OAuth issuer. The
plugin's generated fallback entry sets this automatically when
`~/.engram/opencode.json` provides `gatewayUrl`; without that deployment value,
the plugin keeps its behavioral hooks but does not invent an MCP endpoint. An
explicit config entry must include `oauth: false`.

For a standalone repository whose directory name matches a registered gateway
route, the plugin can be used with zero repository mappings. Keep the explicit
gateway entry when the route is an alias, the checkout is an umbrella
directory, or the route differs from the directory name:

```json
{
  "plugins": ["/path/to/engram/opencode-plugin"],
  "mcp": {
    "servers": {
      "engram": {
        "type": "remote",
        "url": "http://127.0.0.1:8896/mcp/<project-route>",
        "oauth": false,
        "disabled": false
      }
    }
  }
}
```

The global plugin does not generate an MCP route for an unconfigured non-Git
root such as a home or chat directory. Add a `repositories.directories` mapping
when a non-Git workspace is intentionally an Engram project; this prevents
ordinary OpenChamber storage roots from becoming guaranteed gateway 404s.

The direct `mcp.servers.engram` entry is the route authority. The plugin's
generated entry is only a fallback:

```json
{
  "mcp": {
    "servers": {
      "engram": {
        "type": "remote",
        "url": "http://127.0.0.1:8896/mcp/<project-route>",
        "oauth": false,
        "disabled": false
      }
    }
  }
}
```

Users do not register Hindsight, CocoIndex, or Serena separately. Do not add a
second gateway entry through OpenChamber's Settings -> MCP; OpenChamber should
use the OpenCode server's resolved configuration.

### Configured worktree indexes

The plugin reads deployment-local policy from `~/.engram/opencode.json`. No
project receives automatic indexing implicitly; list each route explicitly:

```json
{
    "worktreeIndex": {
      "projects": ["<project-route>"],
      "mcpUrl": "http://127.0.0.1:7999/mcp",
      "embedding": "local/potion-code-16m-v2",
      "allowedRoots": ["/absolute/path/to/authorized-checkout"],
      "familyRoots": {
        "<shared-family>": ["/absolute/path/to/family-checkout"]
      },
      "aggregateRoots": ["/absolute/path/to/authorized/worktree-pool"],
      "gateSessions": true
  }
}
```

For every configured route, the plugin watches OpenCode's
`worktree.updated` events and calls the loopback zvec-grep MCP endpoint's
`zvec_grep_index` tool for each sibling worktree that does not have a usable
local index. It also reconciles the worktree inventory when the plugin starts,
so a worktree created while the event stream was disconnected is not missed.
The canonical checkout is left unchanged. The container mounts the authorized
host roots and writes each sibling's `.zvec-grep` index in place; source and
embedding execution stay inside the container.

The default management endpoint is `http://127.0.0.1:7999/mcp`; set
`worktreeIndex.mcpUrl` or `ZVEC_GREP_MCP_URL` when the container uses another
loopback address. The zvec supervisor must run the `full` MCP toolset because
the `agent` toolset intentionally omits index lifecycle operations. The
container's scheduler deduplicates same-root requests and queues independent
roots, so separate OpenCode sessions do not contend on a host `zg` process.

For configured routes, every zvec workspace-read tool also performs a
synchronous readiness check for its requested absolute `root` immediately
before the MCP call. The root must be the canonical checkout, a worktree
returned by OpenCode's project inventory, or an exact path in
`worktreeIndex.allowedRoots`. Non-Git aggregate roots must additionally be
listed in `aggregateRoots`; arbitrary paths, symlink escapes, bare repositories,
and unconfigured OpenChamber storage directories fail closed. The explicit
index tool is not intercepted; routes without `worktreeIndex` configuration
remain unchanged.

For a shared repository family, `worktreeIndex.familyRoots` is the default
deployment policy: every route whose resolved `family` matches the key inherits
the exact sibling checkout roots. `allowedRoots` remains available for global
or one-off roots. The current checkout is authorized automatically, but family
identity alone does not authorize a sibling root; list every family checkout
that agents must compare (including any release mirrors). Use `aggregateRoots`
only for explicitly managed non-Git aggregate indexes. This is separate from
Serena's project mount: a route such as `/mcp/kubernaut-operator` remains
pinned to `kubernaut-operator`, so cross-repository reads should use
root-scoped Engram code search rather than attempting
`activate_project("kubernaut")` on that mount.

The managed zvec search route also requests `freshness: "wait_for_fresh"` with
`autoUpdate: true`. A search therefore refreshes only its requested workspace
and waits for the pending index work to finish before returning results; Engram
does not keep every managed repository continuously fresh. Direct zvec-grep
clients that bypass the Engram gateway retain zvec-grep's normal eventual
freshness default.

The background inventory pass leaves the canonical checkout unchanged; a
gated session may initialize or refresh its current root synchronously when
that root is the canonical checkout.

When `gateSessions` is explicitly true, the plugin also installs a V2
prompt-admission gate. The first prompt for each session waits for the `engram`
MCP server to report `connected`, then waits for the container's
`zvec_grep_index` operation and the provenance sidecar to finish for the
current checkout. A failed MCP connection or index operation rejects prompt
admission with an actionable error instead of allowing the agent to work
against an unindexed tree. Concurrent prompts share the same readiness
operation. This is intentionally a prompt hook rather than a model-context
hook: the prompt is not admitted until the prerequisite is complete.

Session gating is opt-in. Listing a route in `projects` continues to enable
background worktree indexing, but does not make an optional Engram or zvec-grep
failure block model requests.

ZG's manifest does not record Git branch, commit, or dirty-worktree provenance.
After a successful setup, the plugin therefore writes an Engram-owned
`.zvec-grep/engram-git-provenance.json` sidecar. A missing sidecar, a changed
branch/commit/dirty state, or a provenance record for another root invokes
`zvec_grep_index(root, rebuild: true)` before the worktree is considered ready.
The container scheduler serializes and deduplicates index operations, and a
checkout that changes during a rebuild gets a follow-up rebuild rather than
being marked fresh with the old revision.
Readiness has a finite retry budget and provenance persistence failures reject
the gate instead of retrying forever.

There is no host `zg` executable or host Jieba configuration in this path. The
container image supplies the matching runtime and dictionary. Persistent
endpoint, root-allowlist, and image configuration belongs under the deployment
supervisor and `~/.engram/`.

The container supervisor uses mutable `latest` as the update channel. Every
startup pulls both images first, starts the replacement with pulling disabled,
health-checks it, and restores the previous local image IDs if the replacement
is unhealthy. Image publication is controlled by the zvec-grep release
workflow; no separate digest approval file is required in Engram.

```jsonc
{
  "plugins": [{
    "package": "/path/to/engram/opencode-plugin",
    "options": {
      "worktreeIndex": {
        "projects": ["<project-route>"],
        "mcpUrl": "http://127.0.0.1:7999/mcp",
        "embedding": "local/potion-code-16m-v2"
      }
    }
  }]
}
```

OpenChamber-created worktrees are covered when OpenChamber delegates worktree
operations to the OpenCode server. A client that creates Git worktrees outside
OpenCode's worktree API does not emit this event; the next session prompt still
performs the synchronous current-root readiness check, but an external
Git-level hook is needed if indexing must begin before a session exists.

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
  "plugins": [{
      "package": "/path/to/engram/opencode-plugin",
      "options": {
        "family": "<shared-family>",
        "branchRoutes": {
          "main": "<project-route>",
          "release/vX.Y": "<project-route>-vX.Y"
        }
      }
  }]
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

The loopback/no-OAuth configuration is a single-user host boundary, not an
authorization mechanism for a shared workstation or remotely reachable
OpenCode server. Keep all three listeners on loopback or add an authenticated
reverse proxy and an OS-level service account boundary before exposing them.

If OpenChamber is connected to a remote OpenCode/OpenChamber server,
`127.0.0.1` refers to that server. The gateway must be reachable from the
machine running the OpenCode server, not merely from the browser or mobile
client.

After restarting `io.vectorize.engram-runtime` or the native gateway, reconnect
the Engram MCP entry from `/mcps` (or reload the OpenCode session). Existing
client sessions do not automatically recover their process-bound MCP
connection. A subsequent `MCP server "engram" is not connected` or
`ConnectionRefused` from a tool call is therefore a stale client-session
condition, not the original zvec-grep `backend 'host' failed:` result. First
verify `POST /mcp/<project-route>` with `tools/list`, then reconnect the
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

The explicit `mcp.servers.engram` route remains in each workspace's
`opencode.json` or `.mcp.json`.
The global plugin supplies the hooks and uses the repository mapping to keep
the system-recall identity aligned with the route.

## Migration

Remove old direct MCP entries for `hindsight-docs`, `hindsight-issues`,
`cocoindex-code`, and `serena` after enabling the plugin. Keep one explicit
`mcp.servers.engram` gateway entry when using the hybrid setup. Leaving the old
backend entries in place causes duplicate tools and bypasses the gateway's
backend isolation. Keep the native V2 `mcp.servers.engram` shape shown above.

OpenChamber can inspect the resulting gateway entry through Settings -> MCP,
but the OpenCode configuration remains the source of truth for the route and
the plugin mappings.
