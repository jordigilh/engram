# Python parity fixtures

Each `*.json` pair is generated from the Python reference gateway
(`src/engram/pipeline/engram_gateway.py`), not hand-written:

- `*_input.json` is the upstream MCP `tools/call` result.
- `*_expected.json` is what the Python gateway returns to the client.

Regenerate from the repository root:

```bash
PYTHONPATH=src python3 \
  rust/crates/engram-mcp/tests/fixtures/parity/generate.py
```

Coverage:

- recall: bounded 8-of-10, structuredContent-only, explicit nulls, source
  truncation (`true`/`1`), non-object record, falsy labels, blank/empty text,
  whitespace separators, string/empty-string tags, integer/float/bool scores,
  rounding ties, structured-vs-text precedence, isError passthrough.
- pattern truncation: overlong, exact length, max+1, multiple/non-text items,
  multibyte text, non-list content, zero limit.

The Rust suites `tests/parity_shaping.rs` and `tests/gateway_routing.rs`
compare against these files. The generator is not run in CI.
