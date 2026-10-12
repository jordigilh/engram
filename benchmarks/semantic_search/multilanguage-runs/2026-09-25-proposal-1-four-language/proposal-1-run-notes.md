# Proposal 1: source-evidence rerank — four-language paired replay

## Experiment

The candidate reranks only the first `min(requested limit, 10)` fused results;
it does not add candidates or alter recall. It computes query-token coverage
from source (weight 0.50), signature/name/scope (0.30), and documentation
(0.20), then applies a positive score multiplier capped at 2%. Tokenization
uses the existing language-independent `identifierParts` helper. Missing or
non-matching evidence leaves baseline scores/order intact. The implementation
does not read qrels or branch on source language.

The earlier Go result was nDCG@10 `0.588774 -> 0.595722` (+`0.006948`) and
MRR@10 `0.733333 -> 0.712500` (-`0.020833`), with recall and precision
unchanged. The top-10 sets were identical. For `empty-discovery-fails-closed`,
grade-0 `authorizeSelectionDriver` displaced grade-2 `ListWorkflows`, moving
the first relevant result from rank 1 to rank 4 (MRR `1.00 -> 0.25`). A grade-3
answer improved from rank 2 to rank 1 on `validate-workflow-parameters`, but
that did not offset the loss; `WorkflowState.Add` also fell from rank 2 to 3
for `record-discovered-membership`. This is consistent with generic source
term overlap overpowering the query-specific answer evidence.

## TypeScript paired results

Each lane has eight queries and uses its own fixture and qrels. Values are
`nDCG@10 / MRR@10 / Recall@10 / Precision@10`.

| Fixture | Baseline | Candidate | Delta |
|---|---|---|---|
| Go | 0.588774 / 0.733333 / 0.677083 / 0.250000 | 0.588774 / 0.733333 / 0.677083 / 0.250000 | 0 / 0 / 0 / 0 |
| Python | 0.546521 / 0.547917 / 0.689583 / 0.250000 | 0.546521 / 0.547917 / 0.689583 / 0.250000 | 0 / 0 / 0 / 0 |
| Rust | 0.416330 / 0.455556 / 0.560417 / 0.200000 | 0.416330 / 0.455556 / 0.560417 / 0.200000 | 0 / 0 / 0 / 0 |
| TypeScript | 0.426791 / 0.591667 / 0.550000 / 0.200000 | 0.426791 / 0.591667 / 0.550000 / 0.200000 | 0 / 0 / 0 / 0 |

No TypeScript candidate query order changed in any lane. Every per-query delta
in `comparison.json` is zero for all four metrics. The per-query IDs are
`record-discovered-membership`, `reject-undiscovered-workflow`,
`forward-labels-to-discovery`, `preserve-membership-on-retry`,
`interactive-selection-guard`, `validate-workflow-parameters`,
`empty-discovery-fails-closed`, and `copy-selected-workflow-details`.

## Rust unchanged control

The baseline Rust binary was used for both Rust arms (SHA-256
`e7ea4ed592eb6cd456d7a2fe927f1b00a3d86a2b89f729efda1c3b952136c3dd`). Its
baseline and candidate metrics/deltas were identical:

| Fixture | Rust control nDCG@10 / MRR@10 / Recall@10 / Precision@10 | Delta |
|---|---|---|
| Go | 0.471013 / 0.667857 / 0.620833 / 0.225000 | 0 / 0 / 0 / 0 |
| Python | 0.530362 / 0.543750 / 0.639583 / 0.225000 | 0 / 0 / 0 / 0 |
| Rust | 0.430053 / 0.435714 / 0.585417 / 0.212500 | 0 / 0 / 0 / 0 |
| TypeScript | 0.320709 / 0.323958 / 0.570833 / 0.200000 | 0 / 0 / 0 / 0 |

## Gate

No metric regressed. The strict acceptance rule—at least one metric improves
without lowering any other metric—is **not met**: this ablation is neutral on
all metrics and all candidate query orders. This is not an acceptance or
generalization claim.

## Provenance and runner notes

- Base: `5f2c5e490c93652619b3661faa42a439981517fb`; candidate branch:
  `spike/proposal-1-source-evidence`.
- Baseline TypeScript CLI:
  `/Users/jgil/go/src/github.com/jordigilh/zvec-grep-lexical-baseline/dist/cli/index.js`
- Candidate TypeScript CLI:
  `/Users/jgil/go/src/github.com/jordigilh/zvec-grep-proposal-1-source-evidence/dist/cli/index.js`
- The CLI entrypoint SHA is `817ff1c4ff0b57b0c27eb9c3d8869f9a4db32d1765267798e77c3be6d1b49f6f`
  for both binaries; the entrypoint file is unchanged. The built search files
  differ and are identified by their SHA-256 values below.
- The initial matrix runner's TypeScript diff scope omitted the search pipeline.
  After capture, each `run-manifest.json` was reconciled from the exact
  baseline/candidate worktrees; the final TypeScript candidate diff digest
  includes tracked and untracked files under `src/` and `test/`. The original
  source and built-file SHA-256 values are:
  - `src/engine/pipeline/search/index.ts` —
    `5df9201bfbb92a0221774d08247450f946e2e305ddaeed6a1b9f1fc4ba1f6766`
  - `src/engine/pipeline/search/source-evidence.ts` —
    `360e0ba626d7891a7aff6ef11e81df8104533498df0731875345246086293d27`
  - `test/unit/source-evidence.test.mjs` —
    `96091818cde3cac2822fe14993016852313dd6ecbaffe7a8c130a7a72b1904bf`
  - `dist/engine/pipeline/search/index.js` —
    `b787b63c7ee4664af2d2540507b77b5ab6cbe00de6f191ee462b7d8eb201eb7e`
  - `dist/engine/pipeline/search/source-evidence.js` —
    `6622442ac8c8f546bd57d393f9bea39ade6819fe0e4139aa07ac2dcf4ed59a69`
- The initial runner assigned index versions by arm. Final `run-manifest.json`
  files were reconciled from the worktrees: both TypeScript arms use index v2;
  both Rust arms use the same baseline executable and index v2.
- The successful run used the unique temporary work root
  `/private/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/zvec-grep-proposal-1-four-language.9nAjxl`.
- The Rust candidate argument was intentionally the same baseline Rust binary
  path as the Rust baseline argument.

## Commands

Candidate build and TypeScript checks, from the candidate worktree:

```sh
npm ci
npm run build
npm run lint
npm run format:check
npm run typecheck
npm run test:run:unit
```

Paired matrix command, run from `/Users/jgil/go/src/github.com/jordigilh/engram`:

```sh
python3 -m scripts.replay_multilanguage_zvec \
  --fixtures /Users/jgil/go/src/github.com/jordigilh/engram/benchmarks/semantic_search/fixtures \
  --work-dir /private/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/zvec-grep-proposal-1-four-language.9nAjxl \
  --output-dir /Users/jgil/go/src/github.com/jordigilh/engram/benchmarks/semantic_search/multilanguage-runs/2026-09-25-proposal-1-four-language \
  --baseline-worktree /Users/jgil/go/src/github.com/jordigilh/zvec-grep-lexical-baseline \
  --candidate-worktree /Users/jgil/go/src/github.com/jordigilh/zvec-grep-proposal-1-source-evidence \
  --baseline-typescript /Users/jgil/go/src/github.com/jordigilh/zvec-grep-lexical-baseline/dist/cli/index.js \
  --candidate-typescript /Users/jgil/go/src/github.com/jordigilh/zvec-grep-proposal-1-source-evidence/dist/cli/index.js \
  --baseline-rust /private/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-sense-evidence-baseline-target/debug/zg \
  --candidate-rust /private/var/folders/r7/gktmmltd1zq7wqhsjjslwsm80000gn/T/opencode/zvec-sense-evidence-baseline-target/debug/zg \
  --model-cache /Users/jgil/.engram/zvec-grep/model
```
