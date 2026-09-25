# Go fixture qeval results

The [zvec-grep retrieval issue #6](https://github.com/jordigilh/zvec-grep/issues/6)
tracks experiment decisions and discussion. This page indexes the measured runs
and their local evidence in Engram, where the fixture and evaluator live. The
[structured code-context exploration](https://github.com/jordigilh/zvec-grep/issues/7)
measures a different question: whether retrieved evidence is useful to an LLM.
The [synthetic evaluation protocol](../../SYNTHETIC_EVALUATION.md) defines how
these runs are built, normalized, and scored.

## Evaluation contract

The locked [`go-workflow-discovery-v1`](./README.md) fixture has 8 queries,
10 Go source files, and 38 source-authored symbol units. Its
[`qrels.json`](./qrels.json) covers every unit at source digest
`94183663d9e5f81b753dde7c460428c2414c46867b936b5cc42f9827dab0827f`.
Results below use `scripts/evaluate_semantic_search.py --k 10`: nDCG is graded;
MRR, recall, and precision treat grades 2–3 as relevant. The cutoff is over
**normalized source units**, not ten raw backend hits: an overlapping chunk can
map to several units. Raw backend ranks are retained separately. This is a
controlled fixture regression check, not a cross-language or production-quality
claim; a group-aware cutoff is still needed for stronger cross-backend claims.

## Combined lexical-field experiments

These results are from the zvec-grep [#6 experiment log](https://github.com/jordigilh/zvec-grep/issues/6).
The lexical-only slice adds language-neutral identifier parts and structural
metadata to a combined `lexical_text` FTS field, while leaving returned source
and vector embedding content unchanged.

| Slice | nDCG@10 | MRR@10 | Recall@10 | Precision@10 | Run / decision |
| --- | ---: | ---: | ---: | ---: | --- |
| Initial zvec-git | 0.402 | 0.713 | 0.571 | 0.200 | [Three-backend replay](./replays/2026-09-23/metrics-k10.json) |
| Combined lexical-only | 0.589 | 0.733 | 0.677 | 0.250 | [Lexical replay](./replays/2026-09-23-zvec-lexical/normalized-runs.json); retained champion |
| Fielded FTS + expansion | 0.490 | 0.650 | 0.529 | 0.188 | [Fielded replay](./replays/2026-09-24-zvec-fielded/normalized-runs.json); regression |
| Fielded + expansion, fixed | 0.536 | 0.729 | 0.610 | 0.225 | [Fixed replay](./replays/2026-09-24-zvec-fielded-fixed/normalized-runs.json); still below champion |
| Lexical-only after revert | 0.589 | 0.733 | 0.677 | 0.250 | [Reverted replay](./replays/2026-09-24-zvec-lexical-reverted/normalized-runs.json); recovered champion |

The fielded/expansion experiment was
[reverted](https://github.com/jordigilh/zvec-grep/issues/6#issuecomment-5819480042).
The separate source-evidence reranker kept the same top-10 unit sets: its
[scored run](./replays/2026-09-24-zvec-source-evidence/metrics-k10.json)
changed nDCG **0.588774 → 0.595722** but MRR **0.733333 → 0.712500**;
recall and precision stayed **0.677083** and **0.250000**. It did not pass the
[no-regression gate](https://github.com/jordigilh/zvec-grep/issues/6#issuecomment-5820007400).

## Rust-only resolved-relationship experiment

The [paired Rust evaluation](https://github.com/jordigilh/zvec-grep/issues/6#issuecomment-5824441520)
used one fresh Rust index with the codegraph available versus unavailable.
All eight top-10 candidate sets stayed the same; only ordering changed.

| Paired Rust run | nDCG@10 | MRR@10 | Recall@10 | Precision@10 |
| --- | ---: | ---: | ---: | ---: |
| Relationship rerank off | 0.471013 | 0.667857 | 0.620833 | 0.225000 |
| Relationship rerank on | 0.424009 | 0.580357 | 0.620833 | 0.225000 |

The [normalized rankings](./replays/2026-09-24-zvec-proposal2-relationship/normalized-runs.json)
and [scored metrics](./replays/2026-09-24-zvec-proposal2-relationship/metrics-k10.json)
contain **both** Rust runs; the paired off run also has
[raw results](./replays/2026-09-24-zvec-proposal2-relationship/zvec-rust-no-graph-baseline.json).
The reranker failed the Go fixture gate. **Do not compare these paired Rust
values to the TypeScript lexical-only champion as though they share a baseline.**

## Recompute and interpret

From the Engram repo root, for a normalized run such as the lexical-only revert:

```sh
python3 scripts/evaluate_semantic_search.py \
  --qrels benchmarks/semantic_search/fixtures/go-workflow-discovery-v1/qrels.json \
  --runs benchmarks/semantic_search/fixtures/go-workflow-discovery-v1/replays/2026-09-24-zvec-lexical-reverted/normalized-runs.json \
  --k 10
```

Issue #6 proposes a separate **deep-candidate rescue** ablation next: measure
relevant-unit recall at depth 50 before changing the top-10 set. Report per-query
and category changes alongside all four aggregate metrics, and compare each
experiment against its **same-implementation** control. Retrieval contracts
remain language agnostic even though this first locked fixture is Go.
