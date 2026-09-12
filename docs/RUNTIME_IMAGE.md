# Runtime Image

`Dockerfile.engram` builds a stateless MCP gateway. It can either proxy one
existing host gateway per route (the legacy `endpoint` setting) or aggregate
multiple MCP backends directly in the image.

## Direct Aggregation

Use the `backends` table when the image should own aggregation:

```toml
[instances.kubernaut.backends.docs]
kind = "http"
url = "http://host.containers.internal:8888/mcp/kubernaut-docs/"

[instances.kubernaut.backends.issues]
kind = "http"
url = "http://host.containers.internal:8888/mcp/kubernaut-issues/"

[instances.kubernaut.backends.code]
kind = "http"
url = "http://host.containers.internal:8891/mcp"
headers = { Host = "localhost:8891" }

[instances.kubernaut.backends.serena]
kind = "http"
url = "http://host.containers.internal:8893/mcp/kubernaut"

[instances.kubernaut.backends.rca]
kind = "http"
url = "http://host.containers.internal:8897/mcp"
headers = { Host = "localhost:8897" }
```

Each configured backend is queried concurrently for `tools/list`; the gateway
merges catalogs, prefixes colliding docs/issues tools, and degrades one dead
backend without taking down the route. HTTP `headers` are optional and are
useful for host adapters that reject the container bridge hostname. `stdio`
backends are also supported when their command and required workspace are
available inside the container.

The complete Kubernaut example, including the `kubernaut-v1.5` route, is in
`docs/runtime-kubernaut.toml.example`.

For the current release policy, `kubernaut` is the main/current-v1.6 route and
`kubernaut-v1.5` is the only separate Kubernaut release route until v1.6 is GA.
DCM and Praxis only need their main routes.

Run it with:

```bash
podman run --rm \
  --add-host host.containers.internal:host-gateway \
  -v "$PWD/docs/runtime-kubernaut.toml.example:/etc/engram/instances.toml:ro" \
  -p 127.0.0.1:8896:8896 \
  quay.io/jordigilh/engram:runtime-latest
```

The existing GitHub Actions workflow publishes a new multi-architecture
`runtime-<commit>` image and updates `runtime-latest` whenever
`Dockerfile.engram` or `src/engram/**` changes.

## Branch-Aware Codex

Codex MCP URLs are static. Use the tracked launcher when working from a
checkout that changes branches:

```bash
/path/to/engram/scripts/engram-codex mcp list --json
/path/to/engram/scripts/engram-codex
```

It applies a per-invocation `-c mcp_servers.engram.url=...` override and does
not edit `~/.codex/config.toml`. A `kubernaut` checkout on `release/v1.5`
selects `/mcp/kubernaut-v1.5`; current `kubernaut`/v1.6 and all DCM/Praxis
checkouts use their main route. `ENGRAM_CODEX_PROJECT` overrides the derived
route.

OpenCode can do the same without a wrapper by configuring exact branch routes
in the Engram plugin:

```json
{
  "family": "kubernaut",
  "branchRoutes": {
    "main": "kubernaut",
    "release/v1.5": "kubernaut-v1.5"
  }
}
```
