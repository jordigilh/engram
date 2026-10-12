# Four-language Sense-inspired lexical baseline

This baseline captures the current TypeScript `zvec-grep` implementation with
the retained Sense-inspired lexical metadata: combined `lexical_text`,
language-agnostic identifier decomposition, unchanged vector content, and no
query expansion or reranking. All four fixture suites use the same built CLI,
`local/potion-code-16m-v2`, CPU indexing/query execution, direct hybrid search,
`refresh=off`, and normalized source-unit `@10` evaluation against their own
complete adjudicated qrels.

| Language | Queries | Judged units | nDCG@10 | MRR@10 | Recall@10 | Precision@10 |
|---|---:|---:|---:|---:|---:|---:|
| Go | 8 | 38 | 0.588774 | 0.733333 | 0.677083 | 0.250000 |
| Python | 8 | 33 | 0.546521 | 0.547917 | 0.689583 | 0.250000 |
| TypeScript | 8 | 33 | 0.426791 | 0.591667 | 0.550000 | 0.200000 |
| Rust | 8 | 39* | 0.416330 | 0.455556 | 0.560417 | 0.200000 |
| **Equal-language macro average** | **32** | — | **0.494604** | **0.582118** | **0.619271** | **0.225000** |

The per-language scores are primary; the macro average gives each fixture equal
weight. This is a synthetic four-language baseline, not evidence of performance
on real repositories or arbitrary languages.

## Artifacts and provenance

The machine-readable summary is [`FOUR_LANGUAGE_SENSE_LEXICAL_BASELINE.json`](./FOUR_LANGUAGE_SENSE_LEXICAL_BASELINE.json).
Each language folder contains `raw-runs.json`, `normalized-runs.json`,
`metrics-k10.json`, and `run-manifest.json` under
`<language>-workflow-discovery-v1/replays/2026-09-24-zvec-sense-lexical-baseline/`.
The run manifests record source digest, binary SHA-256, index version, model,
device, and the dirty implementation patch hash.

*Rust unit count: the canonical Rust fixture has 33 symbol units, but six
indexed `.rs` module-declaration files have no unit entries. One such file was
returned during replay, so the Rust run uses a temporary qrels overlay with six
source-authored grade-0 `module-declarations` units (39 total) to score those
results without dropping them or corrupting rank accounting. The canonical
Rust fixture, manifest, and qrels remain unchanged; the overlay qrels and
manifest are saved beside the Rust replay artifacts.*

## Current source base

- zvec-grep commit: `196a730` (`docs: track fork issues and live parity status`)
- TypeScript engine changes: dirty lexical-only Sense-inspired implementation;
  exact diff SHA-256 is stored in the JSON summary and each run manifest.
- Index version: `2`
- Embedding model: `local/potion-code-16m-v2`
- Device: CPU
