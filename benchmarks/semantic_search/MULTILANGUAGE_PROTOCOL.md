# Four-language qeval protocol

This protocol evaluates the same zvec-grep retrieval change independently on
Go, Python, Rust, and TypeScript source. It uses the shared source-unit scorer
and paired arms, but each language keeps its own corpus, symbol universe,
source-authored relevance judgments, digest, and metrics. Do not infer a
language's quality from another language's result or combine units/qrels across
languages.

## Frozen language lanes

| Language | Fixture | Units | Included source |
| --- | --- | ---: | --- |
| Go | [`go-workflow-discovery-v1`](./fixtures/go-workflow-discovery-v1/) | 38 | `**/*.go`, excluding `**/*_test.go` |
| Python | [`python-workflow-discovery-v1`](./fixtures/python-workflow-discovery-v1/) | 33 | `**/*.py`, excluding `**/test_*.py` |
| Rust | [`rust-workflow-discovery-v1`](./fixtures/rust-workflow-discovery-v1/) | 39 | `**/*.rs`, excluding `**/*_test.rs`; includes six grade-0 module-declaration units so every indexed result can be judged |
| TypeScript | [`typescript-workflow-discovery-v1`](./fixtures/typescript-workflow-discovery-v1/) | 33 | `**/*.ts`, excluding `**/*.test.ts` |

Each fixture has its own `manifest.json`, `truth.json`, and complete `qrels.json`.
Qrels positives and grades are authored by inspecting that language's source;
the builder assigns explicit grade 0 to all other manifest units. The eight
query IDs, English prompts, and intent categories are aligned across fixtures,
while symbol IDs, source spans, positive targets, and negative universe are
language-specific. Verify generation and source digests with:

```sh
python3 -m pytest -q tests/test_synthetic_semantic_search.py
```

Do not edit an existing fixture's source, manifest, truth, or query text after
scoring. Create a new fixture version instead.

## Paired implementation arms

For each language, compare each implementation against its **own** control:

- TypeScript baseline at zvec-grep `196a730` (pre-Sense lexical projection)
  versus the Sense-inspired TypeScript combined lexical metadata projection.
- Rust baseline at `196a730` (workspace index v2) versus the Rust
  qualified-name/identifier-parts FTS projection (index v3).

Within each pair, hold fixture revision/digest, qrels, model snapshot, CPU
device, source selection, query text/order, retrieval flags, result limit,
normalizer, and scorer fixed. Build fresh indexes for both arms. Candidate and
baseline artifacts have one output directory per language and arm. The runner
also captures each source/qrels/manifest digest, binary hash, implementation
diff hash, index version, model snapshot, execution flags, and output-artifact
hashes in `run-manifest.json`.

## Reproduce the paired matrix

Build the exact baseline and candidate binaries first:

```sh
( cd "$BASELINE_WORKTREE" && npm ci && npm run build )
( cd "$CANDIDATE_WORKTREE" && npm ci && npm run build )
( cd "$BASELINE_WORKTREE/rust" && cargo build --locked -p zg )
( cd "$CANDIDATE_WORKTREE/rust" && cargo build --locked -p zg )
```

On macOS, provide the SDK libc++ include flags to Cargo if the local C++ toolchain
does not find `<cstdlib>`. Then run from the Engram checkout root:

```sh
python3 -m scripts.replay_multilanguage_zvec \
  --fixtures benchmarks/semantic_search/fixtures \
  --work-dir /tmp/engram-four-language-workspaces-NEW \
  --output-dir benchmarks/semantic_search/multilanguage-runs/NEW-RUN-ID \
  --baseline-worktree "$BASELINE_WORKTREE" \
  --candidate-worktree "$CANDIDATE_WORKTREE" \
  --baseline-typescript "$BASELINE_WORKTREE/dist/cli/index.js" \
  --candidate-typescript "$CANDIDATE_WORKTREE/dist/cli/index.js" \
  --baseline-rust "$BASELINE_WORKTREE/rust/target/debug/zg" \
  --candidate-rust "$CANDIDATE_WORKTREE/rust/target/debug/zg" \
  --model-cache "$HOME/.engram/zvec-grep/model"
```

Use new, empty work/output directories on each invocation. The runner stages
only source paths admitted by each fixture manifest, verifies their digest,
builds each fresh index with direct CPU hybrid settings and
`local/potion-code-16m-v2`, then replays all eight fixed queries at raw limit
10. Its normalized-unit `@10` score and metric definitions are documented in
[`SYNTHETIC_EVALUATION.md`](./SYNTHETIC_EVALUATION.md).

The runner writes `raw-runs.json`, `normalized-runs.json`, `metrics-k10.json`,
and `run-manifest.json` for every arm plus a top-level `comparison.json` with
per-language aggregate and per-query deltas. It refuses to reuse output and
index roots so stale generations cannot silently enter a paired comparison.

## Acceptance and interpretation

Apply the no-regression check **separately per source language and engine**:
at least one of nDCG@10, MRR@10, Recall@10, or Precision@10 must improve while
the other three do not decline; review all eight per-query deltas before
acceptance. Any lane with a metric regression blocks a claim of universal
multi-language improvement. Report the failing lane/query instead of averaging
it away. These 8-query synthetic lanes are development regression evidence,
not proof of real-repository generalization or LLM answer correctness.
