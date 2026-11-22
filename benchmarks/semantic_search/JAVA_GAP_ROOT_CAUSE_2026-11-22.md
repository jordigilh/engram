# Java gap root cause: model and fusion (2026-11-22)

Correction to the interpretation in `THREE_TOOL_COMPARISON_2026-11-21.md`. That
report left zvec-grep's Java lane (0.3250 nDCG, last of the three tools) as a
source-documentation effect. Re-measurement on the same fixtures and qrels shows
it is an engine-configuration effect: the embedding model and the lexical/vector
balance during fusion. The "source-documentation" reading of
`zvec-grep#58` is withdrawn.

Fixtures and qrels are unchanged (sha256 pins intact). Per-language numbers
only; no cross-language averages.

## Ablation (Java lane, k=10, 8 queries)

| Configuration | nDCG@10 | MRR@10 | recall@10 |
|---|---:|---:|---:|
| potion-code-16m + hybrid (the prior floor) | 0.3250 | 0.4792 | 0.4702 |
| potion-code-16m + vector only | 0.3981 | 0.2917 | 0.6223 |
| all-MiniLM-L6-v2 + hybrid | 0.3710 | 0.3482 | 0.6256 |
| all-MiniLM-L6-v2 + vector only | 0.5347 | 0.6688 | 0.6494 |
| Sense v1.15.1 (refreshed run) | 0.4227 | 0.5458 | 0.4515 |

Two separable causes:

1. **Embedding model.** `local/potion-code-16m-v2` is code-domain while these
   queries are English prose. Under identical fusion the model swap is worth
   +0.046 nDCG on Java (+0.144 Python, +0.076 TypeScript, +0.049 Rust, -0.013
   Go).
2. **Fusion weighting.** zvec-grep weighted BM25 above the vector leg
   (`FTS_RRF_WEIGHT 1.1` vs `VECTOR_RRF_WEIGHT 1.0`). For a question such as
   "Where are remediation workflow parameters validated before execution?"
   that favours units repeating the question over units answering it. With a
   stronger model, dropping the lexical leg moved Java MRR 0.348 -> 0.669.

## What Sense does differently

From `luuuc/sense` @ `a678631`:

- `internal/search/shape.go` classifies the query as identifier /
   natural_language / mixed and weights fusion by shape, flooring the vector
   weight for natural language. Its hybrid therefore equals its semantic arm on
   these queries.
- No cross-encoder (`internal/search/ranking.go:52`).
- Its embedding input is a graph-derived context header plus up to 10 body
  lines, not the full body. Projecting zvec-grep's body text away on the same
  fixture made Java *worse* (0.2870 nDCG), so indexed-text shape was not the
  cause of the gap.

## Fix

`zvec-grep#67` ports shape-aware fusion weights (identifier 1.1/1.0, mixed
1.0/1.0, natural language 1.0/2.0) and adds per-repository model selection: a
workspace embeds with one model, so a fresh workspace with no explicit model,
no stored model and no configured default picks the measured-best model for its
dominant source language (tie/unknown -> the generalist
`gte-modernbert-base`). The table comes from the twelve-model sweep in
`zvec-grep#66`.

Resulting zvec-grep versus Sense, nDCG@10 / MRR@10, with the selected model:

| Lane | Model | zvec-grep | Sense |
|---|---|---|---|
| go | gte-modernbert-base | **0.6461** / **0.8438** | 0.5763 / 0.7304 |
| python | multilingual-e5-small | **0.7596** / **0.9063** | 0.5258 / 0.5938 |
| rust | jina-embeddings-v2-base-code | **0.7224** / **0.8063** | 0.5312 / 0.7812 |
| typescript | gte-modernbert-base | **0.6689** / **0.8438** | 0.5164 / 0.6970 |
| java | qwen3-embedding-0.6b | **0.5348** / **0.6563** | 0.4227 / 0.5458 |

zvec-grep leads Sense on nDCG and MRR in all five lanes. Models remain
tool-native, so this is not a controlled model comparison. Against the
pre-change floors, 19 of 20 cells improve and the twentieth ties.

## Caveats

- All five lanes are `role: "tuning"`; there is no holdout, so the model choice
  is selected on the data it is scored on.
- No single model cleared all 20 per-metric floors; per-repository selection
  does. In a polyglot repository only the dominant language gets its best
  model, and the others stay indexed under it at up to ~0.13 nDCG below their
  own best. That trade is accepted deliberately, and per-language routing is
  still open in `zvec-grep#66`.
- An existing index keeps its stored model while the new fusion weights apply,
  so it needs one `zg --index --rebuild` to pick up the default.

Detailed write-up, including the twelve-model sweep:
`zvec-grep` `benchmarks/quality-lanes/SHAPE_AWARE_FUSION_2026-11-22.md`.
