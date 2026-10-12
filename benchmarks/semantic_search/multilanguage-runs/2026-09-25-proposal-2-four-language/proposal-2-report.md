# Proposal 2: four-language relationship-rerank ablation

## Result and gate

The Rust relationship reranker preserved each fixture's existing top-10 set, but the cross-language no-regression gate **fails**. Go and Python both lost nDCG@10 and MRR@10. Rust and TypeScript improved both ranking metrics. Recall@10 and Precision@10 are unchanged in every Rust pair, as expected for an ordering-only rerank.

| Fixture language | Rust baseline: nDCG / MRR / Recall / Precision | Rust candidate | Delta |
| --- | --- | --- | --- |
| Go | 0.471013 / 0.667857 / 0.620833 / 0.225000 | 0.464221 / 0.663690 / 0.620833 / 0.225000 | -0.006792 / -0.004167 / 0 / 0 |
| Python | 0.530362 / 0.543750 / 0.639583 / 0.225000 | 0.527688 / 0.526389 / 0.639583 / 0.225000 | -0.002674 / -0.017361 / 0 / 0 |
| Rust | 0.430053 / 0.435714 / 0.585417 / 0.212500 | 0.443071 / 0.498214 / 0.585417 / 0.212500 | +0.013017 / +0.062500 / 0 / 0 |
| TypeScript | 0.320709 / 0.323958 / 0.570833 / 0.200000 | 0.344953 / 0.400149 / 0.570833 / 0.200000 | +0.024244 / +0.076190 / 0 / 0 |

Metric order in each cell is nDCG@10 / MRR@10 / Recall@10 / Precision@10. Scores are fixture-specific and are not macro-averaged. The TypeScript engine's baseline/candidate deltas are zero on all four fixtures because both TS arms used the specified baseline TypeScript binary.

Only two query rankings changed in Go: `reject-undiscovered-workflow` (nDCG -0.015098, MRR -0.033333) and `forward-labels-to-discovery` (nDCG -0.039238, MRR unchanged). Python changed on those same queries: -0.112048 / -0.138889 and +0.090654 / 0. Rust changed only `forward-labels-to-discovery`: +0.104140 nDCG and +0.500000 MRR. TypeScript changed on `reject-undiscovered-workflow`: -0.046439 / -0.057143, and `forward-labels-to-discovery`: +0.240391 / +0.666667. All other per-query metric deltas are zero; `comparison.json` contains the complete per-query vectors.

The gate is not met: Go and Python regress in both ranking metrics, despite the Rust and TypeScript fixture gains. Keep the lexical/fusion ordering as the default when graph evidence is unavailable or uncertain; do not treat this ablation as an accepted ranking change.

## Implementation and provenance

- Candidate: worktree `zvec-grep-proposal-2-relationship-rerank`, branch `spike/proposal-2-relationship-rerank`, base/HEAD `5f2c5e490c93652619b3661faa42a439981517fb`.
- Baseline: `zvec-grep-lexical-baseline` at the same commit.
- Candidate Rust diff SHA-256, including the untracked relationship module: `47d6828f5b5c5dbc3963edfd98fc8300a549c0a6d5eb8a1d68f17995dc94a701`.
- Each fixture used its own adjudicated qrels; the replay runner reproduced and checked them before scoring.
- The candidate graph sidecar was generated for each fixture. The reranker required a unique typed symbol match, exact indexed/current/graph file hash agreement, exact source range agreement, and a resolved edge with no ambiguous candidates. Calls, references, and parent-child links were eligible; influence was capped at 15% of the best original top-10 score. The candidate universe was not expanded.
- Direct comparison of normalized Rust baseline/candidate runs confirms identical top-10 unit sets for all eight queries in all four fixtures.
- The matrix runner initially assigned Rust index version by arm and omitted
  indexed-search paths from its diff hash. Final per-arm manifests were
  reconciled against the exact worktrees: both Rust arms use index v2 and the
  complete candidate diff includes the indexed-search implementation. The
  separate candidate source-bundle hash above is retained as an independent
  provenance check.

## Rust validation

- `cargo test -p zg-engine --lib`: 438 passed, 10 ignored.
- Focused rerank tests: 3 passed (fixed candidate set/cap, stale-hash fallback, typed same-file parent uniqueness, and uncertain-edge abstention).
- `cargo clippy -p zg-engine --lib -- -D warnings`: passed.
- `cargo fmt --all -- --check`: passed.
- Candidate binary built with the requested unique `CARGO_TARGET_DIR` and `CXXFLAGS`.

## Artifacts

- `comparison.json`: all engine/language aggregate and per-query deltas.
- For each language (`go`, `python`, `rust`, `typescript`) and arm (`rust-baseline`, `rust-candidate`, `typescript-baseline`, `typescript-candidate`): `raw-runs.json`, `normalized-runs.json`, `metrics-k10.json`, and `run-manifest.json`.
- This report: `proposal-2-report.md`.

## Commands

Commands below ran in the indicated directories. `DYLD_LIBRARY_PATH` points at the existing native zvec dylib for macOS test and runtime loading; it does not write to the baseline worktree.

Candidate build (`.../zvec-grep-proposal-2-relationship-rerank/rust`):

```sh
DYLD_LIBRARY_PATH=/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal-matrix-base-target/debug CARGO_TARGET_DIR=/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal2-relationship-rerank-target-20260925-4c7c92c1 CXXFLAGS='-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk -isystem /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/include/c++/v1' cargo build -p zg --bin zg
```

Rust checks (`.../zvec-grep-proposal-2-relationship-rerank/rust`):

```sh
cargo fmt --all
DYLD_LIBRARY_PATH=/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal-matrix-base-target/debug CARGO_TARGET_DIR=/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal2-relationship-rerank-target-20260925-4c7c92c1 CXXFLAGS='-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk -isystem /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/include/c++/v1' cargo test -p zg-engine --lib pipelines::indexed_search::relationship::tests
DYLD_LIBRARY_PATH=/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal-matrix-base-target/debug CARGO_TARGET_DIR=/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal2-relationship-rerank-target-20260925-4c7c92c1 CXXFLAGS='-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk -isystem /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/include/c++/v1' cargo clippy -p zg-engine --lib -- -D warnings
cargo fmt --all -- --check
DYLD_LIBRARY_PATH=/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal-matrix-base-target/debug CARGO_TARGET_DIR=/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal2-relationship-rerank-target-20260925-4c7c92c1 CXXFLAGS='-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk -isystem /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/include/c++/v1' cargo test -p zg-engine --lib
```

Paired four-language replay (candidate worktree cwd; unique temp work root; all report artifacts written to this directory):

```sh
PYTHONPATH=/Users/jgil/go/src/github.com/jordigilh/engram DYLD_LIBRARY_PATH=/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal-matrix-base-target/debug python3 /Users/jgil/go/src/github.com/jordigilh/engram/scripts/replay_multilanguage_zvec.py --fixtures /Users/jgil/go/src/github.com/jordigilh/engram/benchmarks/semantic_search/fixtures --work-dir /var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal2-four-language-qeval-20260925-4c7c92c1 --output-dir /Users/jgil/go/src/github.com/jordigilh/engram/benchmarks/semantic_search/multilanguage-runs/2026-09-25-proposal-2-four-language --baseline-worktree /Users/jgil/go/src/github.com/jordigilh/zvec-grep-lexical-baseline --candidate-worktree /Users/jgil/go/src/github.com/jordigilh/zvec-grep-proposal-2-relationship-rerank --baseline-typescript /Users/jgil/go/src/github.com/jordigilh/zvec-grep-lexical-baseline/dist/cli/index.js --candidate-typescript /Users/jgil/go/src/github.com/jordigilh/zvec-grep-lexical-baseline/dist/cli/index.js --baseline-rust /var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal-matrix-base-target/debug/zg --candidate-rust /var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-proposal2-relationship-rerank-target-20260925-4c7c92c1/debug/zg --model-cache /Users/jgil/.engram/zvec-grep/model --candidate-rust-graph
```
