# Rust gateway shadow run (issue #224)

Date: 2026-11-24
Registry: `~/.engram/runtime/native-instances.toml` (22 projects)
Python: `engram-gateway --config native-instances.toml --port 8896`
Rust: `engram-mcp --config native-instances.toml --port 8899`

The live Python gateway on 8898 still uses the legacy per-project
cocoindex registry, so the comparison used a second Python process on 8896
with the same `native-instances.toml` the Rust gateway loads. Both were
read-only; no writes were sent.

## Tool catalogs

All 22 routes match exactly:

| Project | Tools |
| --- | ---: |
| kubernaut | 38 |
| kubernaut-operator | 38 |
| kubernaut-demo-scenarios | 38 |
| kubernaut-console | 39 |
| kubernaut-docs | 27 |
| engram | 17 |
| kuadrant | 2 |
| kubernaut-operator-tls-e2e-lanes-498 (stdio Serena) | 26 |
| project-lightpath | 23 |
| praxis | 40 |
| praxis-ai | 40 |
| praxis-benchmarks | 40 |
| praxis-demos | 23 |
| praxis-experiments | 23 |
| praxis-forge | 40 |
| praxis-grid | 40 |
| praxis-operator | 40 |
| praxis-policy | 23 |
| praxis-conventions | 23 |
| praxis-enhancements | 23 |
| praxis-proxy-github-io | 23 |
| website | 23 |

`DIFFS 0` (no missing or extra names).

## Representative calls

Byte-identical text and structured content between Python and Rust:

| Project | Tool | Chars | SHA-256 prefix |
| --- | --- | ---: | --- |
| kubernaut | `docs_recall` | 5064 | `8cb133943746` |
| praxis | `docs_recall` | 6387 | `319060b8305e` |
| engram | `docs_recall` | 7195 | `4b78836be890` |
| kubernaut-docs | `docs_recall` | 5064 | `8cb133943746` |
| kuadrant | `kuadrant_docs_recall` | 6645 | `93b3fcceeb01` |
| engram | `zvec_grep_search` | 3681 | `7a21d16a2b89` |

## Divergences found and fixed

1. `code` catalogs exposed `zvec_grep_rg`. Python treats it as a zvec
   management tool and hides it. Rust now hides it too.
2. Serena catalogs were missing `search_for_pattern`. Python's
   `RELEVANT_SERENA_TOOLS` includes it; Rust's filter now does as well.
3. Code-only routes gained a `search_for_pattern` alias backed by
   `zvec_grep_rg`. Python never exposes that alias. Hiding `zvec_grep_rg`
   removes it from the live catalog; the internal `zg` test key still covers
   the translation path.

The stdio route (`kubernaut-operator-tls-e2e-lanes-498`) completed its
`tools/list` through the Rust stdio adapter, confirming the Phase 3 transport
works against the live configuration.

## Remaining before cutover

- Serena `search_for_pattern` overflow shaping (#213).
- Cutover/rollback runbook and launchd switch; Python stays as rollback.
