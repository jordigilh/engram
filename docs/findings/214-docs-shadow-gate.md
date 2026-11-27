# Docs shadow gate (#214 slice 3)

Measured comparison of the real CocoIndex docs flow with the native docs planner on frozen trees. Unexpected divergences: **0**.

## Provenance

| | Before | After |
|---|---|---|
| Source revision | `2fe0bce497d85701ba89b52079ab5a0bd31c1a8b` | same |
| Source commit | `2fe0bce497d85701ba89b52079ab5a0bd31c1a8b` | `2fe0bce497d85701ba89b52079ab5a0bd31c1a8b` |
| Flow source dirty | false | false |
| Python | 3.14.6 | 3.14.6 |
| CocoIndex | 1.0.26 | 1.0.26 |
| Tree files | 37 | 36 |
| Tree bytes | 1376464 | 1370917 |
| Tree sha256 | `f3d84402cf34e5fa9fb508323441e27823f9ea50cf3b162817f2356e4e520193` | `42bb149cd12d56bb584773b76f3a156fc2d6f95eacfccffe73285dd71c22609e` |
| Source tag | `engram-docs` | `engram-docs` |

Rust source identity: project `engram`, tag `engram-docs`, bank `engram-docs`, include `["**/*.md"]`, exclude `[]`.

## Measured parity

| Scenario | Files (oracle/Rust) | Retains (oracle/Rust) | Matched | Duplicate ids | Deletes expected/emitted | Unexpected |
|---|---|---|---|---|---|---|
| Before | 37/37 | 2709/2709 | 2709 | 10 | - | 0 |
| After | 36/36 | 2697/2697 | 2697 | 10 | - | 0 |
| Before → after deletes | - | - | 12 | - | 12/12 | 0 |

The Python flow has no source-document delete. The expectation is derived from the two oracle retain snapshots: 12 ids disappeared. The native ledger emitted 12 deletes (12 matched).

Mutation scenario: edited CALL_GRAPH_CLUSTERING.md; removed spikes/2026-10-02-opencode-mcp-namespace.md. Each oracle run used a fresh CocoIndex LMDB, so both snapshots are full-flow emissions rather than incremental deltas.

## Divergences

| Scenario / kind | Count |
|---|---|
| `after / duplicate_collapse` | 10 |
| `before / duplicate_collapse` | 10 |

- **duplicate_collapse** (before) `COCOINDEX.md`: oracle emitted 2 retains; ledger keeps the last occurrence (b5cb270cef4070520cfe95ac6f66e38535cf7510180533148c1d8210c51e93fb)
- **duplicate_collapse** (before) `METRICS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (1ff2817b0ccf5fdb618bd39c60b24cc86f40a240c26f062608384bfff3d532b0)
- **duplicate_collapse** (before) `METRICS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (ebd971e029deac8d02c9cda7379076bda9edde2d935d37af71374d2c40e45c36)
- **duplicate_collapse** (before) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 6 retains; ledger keeps the last occurrence (616de78515c278b93b6eb0f63b9bf09616bb7a95384eb1663971ccd32e8f87e7)
- **duplicate_collapse** (before) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 4 retains; ledger keeps the last occurrence (bb6e8635d369ccebc74de177717012ef0612d6a4ea30c731c83946820cbd2fcf)
- **duplicate_collapse** (before) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (e0da939d26ddb24a36a51801b28504de1a205a742c60e3ef3a6b10b07c407c46)
- **duplicate_collapse** (before) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (09af7e913574c4f7106676acf7201353259ea1c1c4f9eb629d8e88d2722c76c1)
- **duplicate_collapse** (before) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (85b4a1a8ebb12ebe7f3a737fd9332bff989e7e40ca39872899e2e217baa9e944)
- **duplicate_collapse** (before) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 6 retains; ledger keeps the last occurrence (9836e12822eeee3a8b65c6512c3a5973661dbebb017a905138ecacb90d2051ae)
- **duplicate_collapse** (before) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (a62907c749f3cecd15a2a22f28254cf2e71f4ca9c84899a366f9a976db46989b)
- **duplicate_collapse** (after) `COCOINDEX.md`: oracle emitted 2 retains; ledger keeps the last occurrence (b5cb270cef4070520cfe95ac6f66e38535cf7510180533148c1d8210c51e93fb)
- **duplicate_collapse** (after) `METRICS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (1ff2817b0ccf5fdb618bd39c60b24cc86f40a240c26f062608384bfff3d532b0)
- **duplicate_collapse** (after) `METRICS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (ebd971e029deac8d02c9cda7379076bda9edde2d935d37af71374d2c40e45c36)
- **duplicate_collapse** (after) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 6 retains; ledger keeps the last occurrence (616de78515c278b93b6eb0f63b9bf09616bb7a95384eb1663971ccd32e8f87e7)
- **duplicate_collapse** (after) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 4 retains; ledger keeps the last occurrence (bb6e8635d369ccebc74de177717012ef0612d6a4ea30c731c83946820cbd2fcf)
- **duplicate_collapse** (after) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (e0da939d26ddb24a36a51801b28504de1a205a742c60e3ef3a6b10b07c407c46)
- **duplicate_collapse** (after) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (09af7e913574c4f7106676acf7201353259ea1c1c4f9eb629d8e88d2722c76c1)
- **duplicate_collapse** (after) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (85b4a1a8ebb12ebe7f3a737fd9332bff989e7e40ca39872899e2e217baa9e944)
- **duplicate_collapse** (after) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 6 retains; ledger keeps the last occurrence (9836e12822eeee3a8b65c6512c3a5973661dbebb017a905138ecacb90d2051ae)
- **duplicate_collapse** (after) `RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`: oracle emitted 3 retains; ledger keeps the last occurrence (a62907c749f3cecd15a2a22f28254cf2e71f4ca9c84899a366f9a976db46989b)

## Reproduce

See `rust/crates/engram-ingest/fixtures/docs_shadow_run.py`.

```bash
~/.engram/venv/bin/python rust/crates/engram-ingest/fixtures/docs_shadow_run.py --rev origin/main
```

Both sides read the same frozen snapshots; the tree sha256 and source revision above identify them.
