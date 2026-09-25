# Proposals 1–4: consolidated four-language qeval report

This report consolidates the dedicated worktree experiments requested for the
four proposals tracked in [zvec-grep issue #6](https://github.com/jordigilh/zvec-grep/issues/6).
Each implementation was isolated on its proposal branch, compared with its
same-engine baseline across the Go, Python, Rust, and TypeScript synthetic
source lanes, and scored at normalized source-unit `@10` against that lane's
source-authored qrels. There is no cross-language pooled score.

The frozen lexical baselines, fixture contracts, and paired runner are in
[`MULTILANGUAGE_PROTOCOL.md`](./MULTILANGUAGE_PROTOCOL.md) and
[`MULTILANGUAGE_PAIRED_RESULTS.md`](./MULTILANGUAGE_PAIRED_RESULTS.md). Per-run
raw/normalized output, metrics, and manifests live under
[`multilanguage-runs/`](./multilanguage-runs/).

## Aggregate deltas

Each row is `candidate − same-engine baseline`. TS-only Proposal 1 and Rust-only
Proposals 2–4 have an unchanged other-engine control arm in their matrix.

| Proposal | Source language | Δ nDCG@10 | Δ MRR@10 | Δ Recall@10 | Δ Precision@10 | Outcome |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 1. Source-evidence rerank (TypeScript) | Go | 0 | 0 | 0 | 0 | Neutral |
|  | Python | 0 | 0 | 0 | 0 | Neutral |
|  | Rust | 0 | 0 | 0 | 0 | Neutral |
|  | TypeScript | 0 | 0 | 0 | 0 | Neutral |
| 2. Resolved-relationship rerank (Rust) | Go | -0.006792 | -0.004167 | 0 | 0 | Regression |
|  | Python | -0.002674 | -0.017361 | 0 | 0 | Regression |
|  | Rust | +0.013017 | +0.062500 | 0 | 0 | Improvement |
|  | TypeScript | +0.024244 | +0.076190 | 0 | 0 | Improvement |
| 3. Gated relationship tie-break (Rust) | Go | 0 | 0 | 0 | 0 | Neutral |
|  | Python | 0 | 0 | 0 | 0 | Neutral |
|  | Rust | 0 | 0 | 0 | 0 | Neutral |
|  | TypeScript | 0 | 0 | 0 | 0 | Neutral |
| 4. Bounded graph-assisted recall (Rust) | Go | 0 | 0 | 0 | 0 | Neutral |
|  | Python | 0 | 0 | 0 | 0 | Neutral |
|  | Rust | 0 | 0 | 0 | 0 | Neutral |
|  | TypeScript | 0 | 0 | 0 | 0 | Neutral |

## Per-proposal interpretation

### Proposal 1 — source-evidence rerank

The TypeScript candidate computes language-neutral token coverage over source,
signature/name/scope, and documentation, capped at a 2% score multiplier. It
reorders only the existing top 10 and adds no candidates. The fresh four-lane
implementation changed no result order or metric in any fixture, so it did not
demonstrate an improvement and fails the improvement gate.

This differs from the earlier Go-only Proposal 1 experiment in
[issue #6 comment 5820007400](https://github.com/jordigilh/zvec-grep/issues/6#issuecomment-5820007400),
which raised nDCG but lowered MRR. Neither variant is accepted.

Run artifacts:
[`2026-09-25-proposal-1-four-language/`](./multilanguage-runs/2026-09-25-proposal-1-four-language/)
· [candidate analysis](./multilanguage-runs/2026-09-25-proposal-1-four-language/proposal-1-run-notes.md).

### Proposal 2 — resolved-relationship rerank

The Rust candidate reranks only the existing top 10. It requires unique typed
symbol matches, exact source range and indexed/current/graph content-hash
agreement, query-aligned endpoints, and resolved/unambiguous typed relationships;
the relationship boost is capped at 15% of the strongest original top-10 score.
The candidate set is unchanged.

The lane gate fails overall: Go and Python lose nDCG/MRR, while Rust and
TypeScript improve nDCG/MRR with recall and precision unchanged. This is a
**candidate for per-language opt-in experiments on Rust/TypeScript source**, not
an unconditional default. First test it alongside the lexical projection; that
combination has not yet been evaluated.

Run artifacts:
[`2026-09-25-proposal-2-four-language/`](./multilanguage-runs/2026-09-25-proposal-2-four-language/)
· [candidate analysis and checks](./multilanguage-runs/2026-09-25-proposal-2-four-language/proposal-2-report.md).

### Proposal 3 — gated relationship tie-break

This candidate prefers query-aligned, resolved/unambiguous relationship endpoints
only when baseline scores are within 5%, and caps adjustments at 2%. In Go,
Python, and Rust it made a small number of raw-result swaps; the normalized
qrels-unit ordering and all four metrics stayed unchanged in every language.
It passes the no-regression condition but fails the improvement condition; it
has no measured retrieval-quality benefit yet.

Run artifacts:
[`2026-09-25-proposal-3-four-language/`](./multilanguage-runs/2026-09-25-proposal-3-four-language/)
· [tie audit and analysis](./multilanguage-runs/2026-09-25-proposal-3-four-language/PROPOSAL3_ANALYSIS.md).

### Proposal 4 — bounded graph-assisted recall

The fresh candidate adds up to four resolved/unambiguous direct-neighbor FTS
routes when a query term matches a graph symbol. Graph-only evidence is kept
separate from ordinary lexical/vector corroboration and falls back when graph
data is absent or stale. The graph route was verified active, but all four
metrics and all per-query qrels metrics were unchanged across the four lanes.
This is neutral and does not pass the improvement gate.

The earlier Go-only Proposal 4 implementation was a separate, regressing
variant (nDCG `0.471013 → 0.371469`, MRR `0.667857 → 0.543750`, recall
`0.620833 → 0.491667`, precision `0.225000 → 0.175000`) and was reverted; see
[issue #6 comment 5825266511](https://github.com/jordigilh/zvec-grep/issues/6#issuecomment-5825266511).
Do not conflate that run with this neutral bounded reimplementation.

Run artifacts:
[`2026-09-25-proposal-4-four-language/`](./multilanguage-runs/2026-09-25-proposal-4-four-language/)
· [candidate provenance](./multilanguage-runs/2026-09-25-proposal-4-four-language/candidate-provenance.json).

## Language-profile implications

Using only current synthetic evidence:

- Go/Python: both lexical projections improve all four aggregate metrics; Proposal 2
  relationship rerank regresses Go/Python.
- Rust source: the Rust lexical projection and Proposal 2 improve all four or
  improve ranking metrics without changing recall/precision. The TypeScript
  lexical projection regresses nDCG/recall/precision.
- TypeScript source: both lexical projections lower recall (the Rust projection
  also lowers precision); Proposal 2 improves nDCG/MRR with recall/precision
  unchanged.
- Proposals 1, 3, and 4 have no current per-language metric gain.

These are opt-in hypotheses, not production approvals. Profile combinations
(for example, the Rust lexical projection plus Proposal 2), actual Praxis or
Kubernaut repositories, and answer-level usefulness remain untested. Use the
workspace/per-language gate proposed in [Code IR design issue #8](https://github.com/jordigilh/zvec-grep/issues/8),
with baseline fallback for every unmeasured or failing combination.

## Implementation checks

Each proposal was built/tested in its own worktree:

| Proposal | Verification recorded by its worktree |
| --- | --- |
| 1 | TypeScript build, ESLint, Prettier, typecheck, unit suite: 284 passed, 1 skipped |
| 2 | Rust engine library suite: 438 passed, 10 ignored; strict Clippy and formatting passed |
| 3 | Rust engine library suite: 441 passed, 10 ignored; strict Clippy and formatting passed |
| 4 | Rust engine library suite: 439 passed, 10 ignored; strict Clippy and formatting passed |

All 64 saved proposal arm/language metric files were independently rescored
against their lane's current qrels. Each candidate snapshot is committed on its
dedicated local proposal branch; none was pushed.

## Provenance notes

Each proposal ran on its own dedicated worktree/branch, from base
`5f2c5e490c93652619b3661faa42a439981517fb`. Final run manifests were reconciled
against the exact worktrees: baseline and candidate Rust index version are v2
for Proposals 2–4; Proposal 1's TypeScript arms use v2; full engine-scoped
tracked/untracked implementation diffs and artifact hashes are recorded. The
worktree changes are uncommitted spikes. The branches and worktrees are:

| Proposal | Branch | Experiment commit | Worktree |
| --- | --- | --- | --- |
| 1 | `spike/proposal-1-source-evidence` | `9b6d0e4` | `/Users/jgil/go/src/github.com/jordigilh/zvec-grep-proposal-1-source-evidence` |
| 2 | `spike/proposal-2-relationship-rerank` | `d1b6263` | `/Users/jgil/go/src/github.com/jordigilh/zvec-grep-proposal-2-relationship-rerank` |
| 3 | `spike/proposal-3-gated-relationship-tiebreak` | `4132459` | `/Users/jgil/go/src/github.com/jordigilh/zvec-grep-proposal-3-gated-relationship-tiebreak` |
| 4 | `spike/proposal-4-graph-assisted-recall` | `7c17b79` | `/Users/jgil/go/src/github.com/jordigilh/zvec-grep-proposal-4-graph-assisted-recall` |

The experiment commits are local to their proposal branches and have not been
pushed. The shared integration worktree was left for concurrent work. Earlier
reverted proposal patches were not recoverable as commits, so these are
re-implementations informed by issue descriptions and prior qevals, not exact
snapshots of the reverted code. The run manifests retain the tested base and
candidate diff hash; the experiment commits now preserve those source snapshots
for future revisits.
