# Runtime Image

`Dockerfile.engram` builds a stateless MCP gateway. It can either proxy one
existing host gateway per route (the legacy `endpoint` setting) or aggregate
multiple MCP backends directly in the image.

## Direct Aggregation

Use the `backends` table when the image should own aggregation:

```toml
[instances."<project>".backends.docs]
kind = "http"
url = "http://host.containers.internal:<docs-port>/mcp/<project>-docs/"

[instances."<project>".backends.issues]
kind = "http"
url = "http://host.containers.internal:<issues-port>/mcp/<project>-issues/"

[instances."<project>".backends.code]
kind = "http"
url = "http://host.containers.internal:<code-port>/mcp"

[instances."<project>".backends.serena]
kind = "http"
url = "http://host.containers.internal:<serena-port>/mcp/<project>"

# Optional Kubernaut RCA is not part of the generic runtime shape. Add it only
# in a project-specific configuration when that backend is intentionally used.
```

Each configured backend is queried concurrently for `tools/list`; the gateway
merges catalogs, prefixes colliding docs/issues tools, and degrades one dead
backend without taking down the route. HTTP `headers` are optional and are
useful for host adapters that reject the container bridge hostname. `stdio`
backends are also supported when their command and required workspace are
available inside the container.

The complete Kubernaut example, including its optional RCA backend and the
`kubernaut-v1.5` route, is in `docs/runtime-kubernaut.toml.example`. It is an
exception-specific example, not the generic template.

Projects without separate release-line backends need only one `[instances]`
entry. Projects with release-specific backends should add an explicit route
for each release line.

Run it with:

```bash
podman run --rm \
  --add-host host.containers.internal:host-gateway \
  -v "$PWD/instances.toml:/etc/engram/instances.toml:ro" \
  -p 127.0.0.1:8896:8896 \
  quay.io/jordigilh/engram:runtime-latest
```

Use a project-specific registry such as
`docs/runtime-kubernaut.toml.example` only when those backends are actually
part of the deployment.

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
not edit `~/.codex/config.toml`. By default, the checkout directory name
is the route. `ENGRAM_CODEX_PROJECT` overrides it for a single invocation, and
`ENGRAM_CODEX_BRANCH_ROUTES` supports generic branch mappings:

```bash
export ENGRAM_CODEX_BRANCH_ROUTES='main=my-project,release/v1.5=my-project-v1.5'
```

The launcher tries the exact branch name, the normalized release suffix, and
`release/<suffix>` in that order. Leave the variable unset when all branches
share one route.

OpenCode can do the same without a wrapper by configuring exact branch routes
in the Engram plugin:

```json
{
  "family": "my-family",
  "branchRoutes": {
    "main": "my-project",
    "release/v1.5": "my-project-v1.5"
  }
}
```

Kubernaut's RCA backend and release-line aliases are an optional project
configuration, not part of the generic launcher or gateway runtime.
