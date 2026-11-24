# Rust gateway parity matrix (issue #224)

Python source of truth: `src/engram/pipeline/engram_gateway.py` and
`tests/test_engram_gateway.py` (117 collected cases, including parametrized
ones). Rust: `rust/crates/engram-mcp`.

Status values:

- **Covered**: the behavior has a Rust test and the response shape matches.
- **Covered, Rust-only**: Rust deliberately extends the behavior; no Python
  equivalent to port.
- **Deferred**: intentionally out of #224.
- **N/A**: the Python code under test is not wired into the gateway.

| Python test class | Cases | Rust status | Rust coverage |
| --- | --- | --- | --- |
| `TestPrefixedToolName` | 4 | Covered | `catalog::tests::docs_and_issues_are_prefixed_and_manual_alias_is_compact`, `duplicate_unprefixed_names_are_qualified` |
| `TestBuildCatalog` | 5 | Covered | `catalog::tests::*` (prefixing, filtering, reservations, duplicate names) |
| `TestRouteCall` | 3 | Covered | `catalog::tests::*`, `gateway_aggregates_tools_and_routes_prefixed_calls` |
| `TestExceptionFormatting` | 2 | Covered | `backend_failure_appends_a_zeroed_error_metric` pins `backend '<key>' failed: <message>`; Rust flattens the cause into the message |
| `TestFilterRelevantTools` | 10 | Covered | `catalog::tests::filters_match_python_sets`, `alias_*`, `managed_policy_targets_raw_names_only` |
| `TestManualMentalModelAdapter` | 3 | Covered | `backend::tests::manual_*` |
| `TestAggregateToolsList` | 6 | Covered | `aggregate::tests::*`, `partial_backend_failure_keeps_survivors_and_projects_are_isolated` |
| `TestHandleToolsCall` | 9 | Covered | `gateway_routing` tests: workspace routing, zvec freshness, prefixed routing, recall normalization, unknown tool, backend error, non-tools method |
| `TestHttpRelayAdapter` | 4 | Covered | `reads_retry_transient_failures_but_writes_do_not`, `write_calls_are_not_replayed_after_an_uncertain_failure`, `sends_negotiated_version_and_follows_tools_pagination`, `http_adapter_runs_one_shot_session_and_forwards_workspace` |
| `TestRecallResponseNormalization` | 4 | Covered | `recall_matches_python_goldens`, `recall_response_is_normalized_to_python_golden`, `non_recall_results_payload_passes_through_unshaped`, `recall_normalization_is_idempotent_for_untruncated_summaries` |
| `TestSerenaPatternResponseNormalization` | 2 | Deferred (#213) | Raw Serena overflow shaping; no live config exposes it |
| `TestEstimateTokens` | 4 | Covered | `metrics::tests::*` |
| `TestExtractResultText` | 4 | Covered | `metrics::tests::extracts_only_text_items_in_order`, `result_chars_counts_characters_not_bytes` |
| `TestGatewayCallMetricsLogging` | 7 | Covered | `metrics::tests::*`, `tools_call_appends_a_gateway_metric_line`, `unknown_tool_does_not_write_a_metric_line`, `backend_failure_appends_a_zeroed_error_metric` |
| `test_project_defaults_to_none_when_not_passed` | 1 | N/A | Rust route names are always present; Python's `project=None` is a unit-test-only path |
| `TestBuildBackendAdapters` | 6 | Covered | `backend::tests::shared_key_*`, `stdio_adapters_without_matching_keys_are_distinct`, registry stdio tests |
| `TestZvecShadowRelayAdapter` | 11 | Deferred (#214) | `shadow_http` retires with CocoIndex; `backend::tests::rejects_shadow_http_at_build_time` pins the decision |
| `TestBuildApp` | 2 | Covered | Lifecycle tests: one route per project, GET declined with 405 |
| `TestLoadInstanceRegistry` | 10 | Covered | `registry::tests::*` |
| `TestStdioSubprocessAdapterCallToolSerialization` | 2 | Covered | `stdio::tests::normalizes_tools_and_strips_optional_fields`, `strips_nested_null_arrays` |
| `TestStdioSubprocessAdapterListToolsSelfHeal` | 2 | Covered | `restarts_once_after_the_child_dies`, `malformed_output_fails_after_one_retry`, `definitive_tools_list_rejection_does_not_restart` |
| `TestPrewarmStdioBackends` | 3 | Covered | `gateway_routes_a_stdio_backend`, prewarm tests in `gateway_routing` |
| `TestPrewarmProjectCatalogs` | 4 | Covered | `tools_call_works_immediately_after_prewarm`, `partial_backend_failure_keeps_survivors_and_projects_are_isolated` |
| `test_response_shaping.py` | 4 | N/A | Tests an unwired `spike/response_shaping.py` |

## Extra Rust-only parity guards

These have no Python test but pin behavior the migration must preserve:

- Empty SSE `data:` prelude is skipped (`backend::tests::sse_and_plain_json_parse`).
- `search_for_pattern` alias schema and truncation goldens.
- Typed `workspace_unbound` / `outcome_unknown` envelopes.
- Session lifecycle, cancellation fencing, Origin/Host policy.

## Post-cutover status (2026-11-24)

1. Shadow run completed: 22/22 catalogs matched and representative calls were
   byte-identical (`docs/RUST_GATEWAY_SHADOW_RUN.md`).
2. Cutover executed: `engram-mcp` serves `:8898`; Python is rollback only
   (`docs/RUST_GATEWAY_CUTOVER.md`).
3. Serena `search_for_pattern` overflow shaping is deferred to #213.
4. Deleting the Python code is blocked on #212, #213, #214, #215 and the
   Hindsight proxy cutover; tracked in #232.
