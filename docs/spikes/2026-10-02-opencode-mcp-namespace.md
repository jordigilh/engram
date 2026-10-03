# OpenCode MCP namespace collision mitigation

**Date:** 2026-10-02  
**OpenCode:** `2.0.18`  
**Scope:** local mitigation and upstream issue triage; no Engram gateway
protocol change

## Finding

The Engram gateway is healthy and advertises raw MCP tool names such as
`docs_retain` and `docs_sync_retain`. The failure is in the OpenCode Code Mode
delegation path: a server-qualified tool path can be qualified a second time,
producing an unknown tool such as:

```text
Unknown tool 'engram.engram.docs_sync_retain'.
```

This is a client/catalog routing failure, not an Hindsight or Engram backend
failure.

## Reproduction evidence

### Existing delegated-worker reproduction

Before the mitigation, a fresh worker in the Engram project was instructed to
make `docs_retain` its first and only MCP call. Worker session
`ses_f01384446ffeu3bTCX72fU1Gr1` stopped before filesystem access with:

```text
Unknown tool 'engram.engram.docs_sync_retain'. Did you mean tools.engram.docs_sync_retain?
```

The same gateway endpoint independently returned HTTP 200 for `initialize` and
`tools/list`, including both `docs_retain` and `docs_sync_retain`.

### Controlled default-Code-Mode control

The committed fixture
`docs/spikes/fixtures/opencode-mcp-namespace/opencode-default-codemode.jsonc`
configures the same remote server without `codemode: false`. Running OpenCode
`2.0.18` in a fresh standalone session and asking the model to execute
`tools.engram.docs_retain(...)` produced:

```text
Unknown tool 'engram.docs_retain'. Did you mean tools.opencode.list_mcp_resources?
```

The control confirms that the Code Mode catalog/dispatcher can lack or
misresolve the expected Engram namespace even while the MCP endpoint itself is
healthy. The earlier worker reproduction captures the stronger double-prefix
form.

## Local mitigation

`opencode-plugin/identity.ts` now emits:

```json
{
  "type": "remote",
  "url": "http://127.0.0.1:8896/mcp/engram",
  "oauth": false,
  "disabled": false,
  "codemode": false
}
```

OpenCode's documented direct-MCP mode exposes stable flat names of the form
`engram_<tool>` and bypasses the failing nested Code Mode namespace. The
generated-entry tests cover this field; the plugin suite passes **80 tests**.
After restart, `opencode mcp list` reports `engram connected`, and a fresh
delegated bootstrap used the direct native `functions.engram_docs_retain` path
successfully (operation ID `e40db724-95f0-4662-b8a0-5b17aa7dc12e`).

Explicit project-level MCP entries remain authoritative and are not overwritten
by the plugin. They must set `"codemode": false` themselves if they bypass the
generated entry.

## Upstream triage

No OpenCode issue matching the exact `engram.engram.*` error was found in the
repository search. The closest existing reports are:

* [#40611](https://github.com/anomalyco/opencode/issues/40611) — Code Mode
  nested-runtime namespace collision; explicitly documents `codemode: false` as
  the workaround. **Closest match.**
* [#41389](https://github.com/anomalyco/opencode/issues/41389) — catalog-listed
  MCP tools fail when invoked outside the `execute` runtime.
* [#45349](https://github.com/anomalyco/opencode/issues/45349) and
  [#45521](https://github.com/anomalyco/opencode/issues/45521) — Code Mode
  discovery/helper namespace forms are advertised inconsistently and produce
  `Unknown tool` errors.

The evidence supports treating this as the same Code Mode namespace/catalog
family as #40611, with a local workaround. Do not file a duplicate issue yet;
if the direct-mode mitigation reproduces a failure, file a new report using the
fixture, OpenCode version, exact `tools.list` catalog, session ID, and sanitized
server log lines.

## Reproduction checklist for a future upstream report

1. Start the Engram gateway and verify raw `tools/list` contains
   `docs_retain`.
2. Use the fixture without `codemode: false` in a fresh standalone OpenCode
   `2.0.18` session.
3. Ask `execute` to call exactly `tools.engram.docs_retain(...)`.
4. Capture whether the result is `engram.docs_retain`,
   `engram.engram.docs_retain`, or a missing namespace.
5. Repeat with `codemode: false`; the call should use the flat
   `engram_docs_retain` native tool and succeed.
6. Include `opencode --version`, `opencode mcp list`, the sanitized
   `mcp connected` log line, and the complete error.
