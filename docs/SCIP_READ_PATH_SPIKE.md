# SCIP read-path spike (issue #204)

**Date:** 2026-11-22
**Status:** Gate 1–2 passed; integration build not started
**Scope:** replace the hot read path (`find_symbol`,
`find_referencing_symbols`, `get_symbols_overview`) with a static SCIP index.
Serena remains the write/diagnostic backend.

## Measured on a kubernaut worktree (`2664-auth-security-events`)

| Measurement | Result |
|---|---|
| `scip-go index --skip-tests ./...` | **5.0 s** |
| SCIP file | 69 MB |
| Indexer peak RSS | 883 MB (one-shot, not resident) |
| SQLite conversion | 46 MB |
| Symbols | 41,600 |
| Mentions | 180,680 |
| Documents | 1,153 |
| `find_symbol("Syncer")` | 5.1 ms |
| Definition location | exact (`pkg/fleet/fmc/syncer.go:139:0-160:1`) |
| Warm p95 (200 lookups + mention count) | **29 ms** |

This is the first topology that satisfies the memory and latency budgets: no
resident gopls, no per-workspace fan-out, no 59–245 s reactivation.

## Query layer

`scip expt-convert` produces SQLite tables: `documents`, `chunks`,
`global_symbols`, `mentions`, `defn_enclosing_ranges`. Definition locations are
exact. References are keyed by chunk (`mentions` → `chunks` → `documents`);
exact reference line/char requires decoding the occurrence blob, a small
protobuf reader (bounded follow-up).

## Integration shape

1. Git hook/watcher runs `scip-go` for the mutated worktree.
2. Converter writes `<worktree>/<HEAD>/index.db`; atomic rename swap.
3. Query is admitted only when the index version matches the workspace epoch
   and resolved HEAD; otherwise return `workspace_flushing` while the 5 s
   regeneration runs.
4. Gateway routes the three read tools to SQLite; Serena keeps writes,
   diagnostics, and the read fallback while an index is regenerating.
5. Dirty worktrees: use Serena fallback or a stash-based snapshot; decide
   before rollout.

## Gates remaining

- Exact reference positions via occurrence decoding.
- Parity ≥95% against live Serena on a sample of ~50 symbols.
- Two-worktree concurrent A/B with commit-in-A while querying B.
- Dirty-worktree policy and CI parity fixtures.

## Decision

Proceed with a 3–5 day integration build for the Go read path. Do not retire
the git-hook restart or the Serena write path until the A/B gates pass.
