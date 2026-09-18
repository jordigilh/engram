# MCP Response Shaping Benchmark

## Scope

This report documents a disabled response-shaping spike in
`spike/response_shaping.py`. It covers lexical JSON-whitespace compaction for
valid JSON text returned by the `docs`, `issues`, and `rca`-style backends.
The spike is not imported or invoked by `src/engram/pipeline/engram_gateway.py`.

It does **not** claim savings for CocoIndex code/search output or Serena output.
Those remain unchanged and are tracked as a separate spike in issue [#101](https://github.com/jordigilh/engram/issues/101).

## Reproduction

The live benchmark requires the local Hindsight service and performs only
read-only `recall` calls. It uses the same `HttpRelayAdapter` as the gateway,
with eight fixed queries against each of four existing banks, then applies the
disabled spike locally for comparison. It reports aggregate sizes and timings
only; it does not print or persist response content.

```bash
.venv/bin/python spike/benchmark_live_hindsight_response_shaping.py
```

The live run uses `2048` recall `max_tokens` by default. Pass `--base-url` to
measure another Hindsight instance.

The synthetic capability benchmark remains reproducible without a live
service:

```bash
.venv/bin/python spike/benchmark_mcp_response_shaping.py
```

It uses five deterministic fixtures and performs 1,000 shaping calls per
fixture after module import. The fixtures contain no live repository, issue,
or user data.

Token counts use Engram's existing `tiktoken` `cl100k_base` estimate. This is a
consistent local comparison, not an exact Anthropic/OpenCode billed-token
count.

Latency measures only the shaping function, excluding MCP transport, backend
latency, tokenization, and model inference.

## Live Results

Measured on 2026-09-18 with the repository `.venv`, against the local Hindsight
service. The run completed with `32` successful samples and `0` errors. Each
response was already a single compact JSON document, so the spike's lossless
whitespace pass had no bytes to remove.

| Bank | Samples | Response form | Median input chars | Median output chars | Input tokens p50/p90/p95 | Output tokens p50/p90/p95 | Saved tokens p50/p90/p95 | Median savings | Remote p50 ms | Remote p95 ms | Shaping p95 ms |
|---|---:|---|---:|---:|---|---|---|---:|---:|---:|---:|
| `kubernaut-docs` | 8 | compact JSON | 34,604 | 34,604 | 9,965 / 10,987 / 11,318 | 9,965 / 10,987 / 11,318 | 0 / 0 / 0 | 0.0% | 2,547 | 3,083 | 5.513 |
| `kubernaut-issues` | 8 | compact JSON | 14,412 | 14,412 | 4,158 / 4,803 / 4,997 | 4,158 / 4,803 / 4,997 | 0 / 0 / 0 | 0.0% | 4,226 | 4,577 | 2.285 |
| `praxis-docs` | 8 | compact JSON | 23,063 | 23,063 | 6,778 / 7,818 / 9,094 | 6,778 / 7,818 / 9,094 | 0 / 0 / 0 | 0.0% | 2,004 | 2,395 | 4.261 |
| `praxis-issues` | 8 | compact JSON | 17,409 | 17,409 | 5,186 / 7,255 / 7,759 | 5,186 / 7,255 / 7,759 | 0 / 0 / 0 | 0.0% | 3,657 | 5,680 | 2.441 |
| **All banks** | **32** | **compact JSON** | **20,458** | **20,458** | **6,175 / 9,965 / 10,119** | **6,175 / 9,965 / 10,119** | **0 / 0 / 0** | **0.0%** | **3,004** | **4,577** | **4.261** |

Across the run, the responses contained an estimated `206,610` input tokens
and saved `0` tokens. Remote timing includes the one-shot MCP relay lifecycle
(initialize, tool call, and cleanup); shaping timing covers only the local
response-shaping function.

## Synthetic Results

Measured on 2026-09-18 with the repository `.venv`:

| Fixture | Strategy | Input chars | Output chars | Input tokens | Output tokens | Saved | Savings | p50 (us) | p95 (us) | Preservation |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Hindsight recall envelope | `json-whitespace` | 2,966 | 2,061 | 874 | 580 | 294 | 33.6% | 166.2 | 188.1 | JSON-equivalent |
| RCA dossier envelope | `json-whitespace` | 2,640 | 1,894 | 770 | 497 | 273 | 35.5% | 150.9 | 166.2 | JSON-equivalent |
| CocoIndex/code text pass-through | `none` | 79 | 79 | 25 | 25 | 0 | 0.0% | 0.1 | 0.1 | Byte-identical |
| Malformed JSON pass-through | `none` | 17 | 17 | 6 | 6 | 0 | 0.0% | 2.3 | 2.8 | Byte-identical |
| Already compact JSON | `none` | 43 | 43 | 14 | 14 | 0 | 0.0% | 4.4 | 4.7 | JSON-equivalent |

## Interpretation

- The live Hindsight sample showed no current token savings because its MCP
  responses were already compact JSON. This is the production-shaped result
  for the tested banks and query matrix, not evidence that the shaper is broken.
- Pretty-printed structured responses saved 33.6% to 35.5% of estimated input tokens in these fixtures.
- Code/search text received no shaping and therefore showed zero savings, as intended.
- Malformed JSON and already compact JSON safely passed through unchanged.
- Synthetic eligible-response shaping stayed below 0.20 ms p50 and 0.20 ms p95; live payload scans were measured separately above and timing varies by host and process state.
- The implementation preserves string contents, key order, duplicate keys, numeric spelling, and all JSON fields because it removes whitespace lexically instead of round-tripping through `json.dumps(json.loads(...))`.

## Limitations

- These are synthetic fixtures, not a production traffic sample. Savings will vary with backend serialization and payload structure.
- The live result is one timestamped sample of four mutable banks and eight broad queries per bank; it is not a long-term traffic distribution.
- `JSON-equivalent` is checked with `json.loads()` in the benchmark harness. The implementation itself is lexical and also preserves representations that a JSON object comparison does not distinguish, such as duplicate keys and numeric spelling.
- The benchmark does not measure whether a model changes behavior, because this change does not remove semantic data and no provider call is made.
- Gateway logs retain only the live input-side `result_chars` and `est_tokens` measurements; the spike is not part of production traffic.

## Recommendation

Keep this as a disabled spike, not as a gateway optimization. Do not claim
token savings for the current Hindsight serialization from the synthetic
fixture results. A future model-facing compaction strategy should be evaluated
against real traffic before being considered for the default path.
Do not infer that code/search or Serena output can be compacted safely from this
report. Any formatter for those surfaces must satisfy issue #101 with its own
fixtures, preservation checks, and benchmark report before being enabled.
