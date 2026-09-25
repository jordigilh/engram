# Multi-language synthetic qeval lanes

Each source language is an **independent evaluation lane**. We use the same
eight question intents to make the lanes comparable, but each language has its
own source tree, manifest/unit universe, source-authored relevance truth,
complete qrels, and per-query metrics. Never pool units or average rankings
across languages. Report each language separately; an optional macro-average
may be shown only after the per-language results and regressions.

## Fixture set

| Source language | Fixture | Source-authored units | Scope |
| --- | --- | ---: | --- |
| Go | [`go-workflow-discovery-v1`](./fixtures/go-workflow-discovery-v1/) | 38 | Existing frozen fixture and historical Go qeval lane. |
| Python | [`python-workflow-discovery-v1`](./fixtures/python-workflow-discovery-v1/) | 33 | Independent Python implementation, including state, filters, validation, retry, interactive selection, and distractors. |
| Rust | [`rust-workflow-discovery-v1`](./fixtures/rust-workflow-discovery-v1/) | 39 | Independent compiled Rust crate using idiomatic structs, impl methods, and `Result` error flow; includes six module-declaration units so every indexed source span is judged. |
| TypeScript | [`typescript-workflow-discovery-v1`](./fixtures/typescript-workflow-discovery-v1/) | 33 | Independent TypeScript implementation using interfaces, classes, typed collections, and functions. |

Each fixture directory contains `manifest.json`, `truth.json`, and generated
`qrels.json`. The source selection is language-specific (`.go`, `.py`, `.rs`,
`.ts`); all files outside the declared include/exclude scope, including tests,
are omitted from the snapshot digest. `build_synthetic_qrels.py` hashes sorted
relative paths and bytes and expands source-authored positive/graded targets to
grade-0 judgments for every other manifest unit.

The qrels are independently authored from each language's source. Similar
architecture does **not** mean copy/paste labels: review whether an idiom or
answer site in that implementation actually supports the same judgment before
regenerating and accepting its qrels. Query IDs/categories are shared to align
intent-level reporting; exact English prompts remain fixed across these v1
fixtures.

## Paired evaluation rule

Evaluate each engine change against its **same-engine, same-language** control:

- TypeScript candidate vs. TypeScript baseline, separately on each language.
- Rust candidate vs. Rust baseline, separately on each language.

Hold the fixture snapshot, qrels, model, device, index/query flags, and unit
normalizer constant within each pair. A Rust run on Go source is a Rust-engine
measurement on the **Go lane**, not a Rust-language result. Likewise, a
TypeScript engine searching Go source is still a Go-language lane.

Run `scripts/replay_synthetic_zvec.py` once for each language × engine build;
the runner verifies the staged root's source digest against that language's
qrels, saves raw and normalized output plus metrics, and rejects reused output
directories. Use a descriptive backend label that names both engine and change.
Store all runs under the language fixture's `replays/` directory, with one
fresh index root per pair/arm. Record engine commit/binary hash, implementation
diff hash, model snapshot, index version, source digest, and search settings in
`run-manifest.json` beside each paired experiment.

For a complete TypeScript/Rust baseline-versus-candidate matrix, use the matrix
runner. It creates a fresh source-only root for every arm, builds the index with
the named arm's binary, verifies the source digest, replays the same eight
queries, and writes the four artifacts per arm plus per-query comparison deltas:

```sh
python3 -m scripts.replay_multilanguage_zvec \
  --fixtures benchmarks/semantic_search/fixtures \
  --work-dir /tmp/engram-four-language-workspaces \
  --output-dir benchmarks/semantic_search/multilanguage-runs/YYYY-MM-DD-change-id \
  --baseline-worktree /absolute/path/to/zvec-grep-baseline-worktree \
  --candidate-worktree /absolute/path/to/zvec-grep-candidate-worktree \
  --baseline-typescript /absolute/path/to/baseline/dist/cli/index.js \
  --candidate-typescript /absolute/path/to/candidate/dist/cli/index.js \
  --baseline-rust /absolute/path/to/baseline/rust/target/debug/zg \
  --candidate-rust /absolute/path/to/candidate/rust/target/debug/zg \
  --model-cache "$HOME/.engram/zvec-grep/model"
```

Build those four exact binaries before replaying. TypeScript worktrees need
`npm ci && npm run build`; Rust worktrees need
`cargo build --locked -p zg` from `rust/` (plus host-specific `CXXFLAGS` if
required). Use distinct output and index-workspace directories for each matrix
run; the runner refuses existing output directories and staged index roots.

The enforced acceptance condition from issue #6 applies **per language**: on a
locked lane, improve at least one of nDCG@10, MRR@10, Recall@10, or Precision@10
without reducing the other three, and inspect all eight per-query deltas. No
weighted overall score can hide a language regression. A lane with no result is
**untested**, not implicitly supported by another language's score.

### Sense-inspired TypeScript lexical baseline

The following baseline runs use the retained TypeScript Sense-inspired lexical
changes (combined `lexical_text`, language-neutral identifier decomposition),
index v2, `local/potion-code-16m-v2`, CPU, and direct hybrid search with
normalized source-unit cutoff 10. Each row uses its own complete adjudicated
qrels and is a baseline for that language lane; it is not a cross-language
generalization claim.

| Language lane | Units scored | nDCG@10 | MRR@10 | Recall@10 | Precision@10 | Run artifacts |
|---|---:|---:|---:|---:|---:|---|
| Go | 38 | 0.588774 | 0.733333 | 0.677083 | 0.250000 | [`replays/2026-09-24-zvec-sense-lexical-baseline`](./fixtures/go-workflow-discovery-v1/replays/2026-09-24-zvec-sense-lexical-baseline/) |
| Python | 33 | 0.546521 | 0.547917 | 0.689583 | 0.250000 | [`replays/2026-09-24-zvec-sense-lexical-baseline`](./fixtures/python-workflow-discovery-v1/replays/2026-09-24-zvec-sense-lexical-baseline/) |
| TypeScript | 33 | 0.426791 | 0.591667 | 0.550000 | 0.200000 | [`replays/2026-09-24-zvec-sense-lexical-baseline`](./fixtures/typescript-workflow-discovery-v1/replays/2026-09-24-zvec-sense-lexical-baseline/) |
| Rust | 39* | 0.416330 | 0.455556 | 0.560417 | 0.200000 | [`replays/2026-09-24-zvec-sense-lexical-baseline`](./fixtures/rust-workflow-discovery-v1/replays/2026-09-24-zvec-sense-lexical-baseline/) |

*When this baseline was captured, the Rust fixture had 33 symbol units and six
indexed `mod.rs`/`lib.rs` module-declaration files were added as grade-0 units in
a temporary qrels overlay. The canonical Rust fixture now includes those six
units (39 total); the paired matrix below uses the canonical 39-unit qrels.*

The equal-language macro average is nDCG@10 **0.494604**, MRR@10 **0.582118**,
recall@10 **0.619271**, precision@10 **0.225000**. Per-language metrics remain
the primary baseline; this average is only a compact summary. A machine-readable
summary with binary and implementation-diff provenance is at
[`fixtures/FOUR_LANGUAGE_SENSE_LEXICAL_BASELINE.json`](./fixtures/FOUR_LANGUAGE_SENSE_LEXICAL_BASELINE.json).

## Paired change evaluation

The same-engine baseline/candidate matrix is archived in
[`multilanguage-runs/2026-09-24-four-language-paired-v4/comparison.json`](./multilanguage-runs/2026-09-24-four-language-paired-v4/comparison.json).
Every arm has raw output, normalized source-unit ranks, metrics, and a
`run-manifest.json`. These measurements answer the source-language question:
**both changes improved all four aggregate metrics on Go and Python.**

| Source language | Change | Δ nDCG@10 | Δ MRR@10 | Δ Recall@10 | Δ Precision@10 | Per-language aggregate gate |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Go | TypeScript Sense lexical projection | +0.1865 | +0.0208 | +0.1063 | +0.0500 | Pass |
| Go | Rust lexical projection | +0.1231 | +0.0759 | +0.0563 | +0.0250 | Pass |
| Python | TypeScript Sense lexical projection | +0.0375 | +0.0349 | +0.0250 | +0.0125 | Pass |
| Python | Rust lexical projection | +0.0243 | +0.0250 | +0.0500 | +0.0250 | Pass |
| Rust | TypeScript Sense lexical projection | -0.0179 | +0.0215 | -0.0250 | -0.0125 | **Fail** |
| Rust | Rust lexical projection | +0.0326 | +0.0219 | +0.0625 | +0.0250 | Pass |
| TypeScript | TypeScript Sense lexical projection | +0.0849 | +0.1740 | -0.0042 | 0.0000 | **Fail** |
| TypeScript | Rust lexical projection | +0.0908 | +0.2506 | -0.0583 | -0.0125 | **Fail** |

“Pass” means only that at least one aggregate metric improved without reducing
the other three on that language fixture. Query-level losses remain important
diagnostics: the Go `copy-selected-workflow-details` query loses useful top-10
units in both candidate arms; Rust-source `record-discovered-membership` drops
sharply for the TypeScript change; and TypeScript-source
`validate-workflow-parameters` loses relevant Rust results. The full per-query
deltas are in `comparison.json`. Neither change passes the four-language gate
because a per-language regression is present; do not replace the separate
per-language rows with the equal-language baseline macro average.

The Rust lane's 39-unit qrels include six explicit grade-0 module-declaration
units from `lib.rs`/`mod.rs`, so indexed module spans remain judged rather than
being discarded or treated as unknown results. The Rust fixture compiler check,
Python fixture compilation, TypeScript fixture typecheck, and benchmark integrity
tests pass. This remains source-authored synthetic evidence, not real-repository
or LLM-answer correctness evidence.

## Rebuild qrels and verify source spans

From the Engram checkout root:

```sh
for language in python rust typescript; do
  fixture="benchmarks/semantic_search/fixtures/${language}-workflow-discovery-v1"
  python3 scripts/build_synthetic_qrels.py --fixture "$fixture" --output "$fixture/qrels.json"
done
python3 -m pytest -q tests/test_synthetic_semantic_search.py
```

The Go v1 qrels remain frozen; the test checks reproducibility for all four
lanes, common query-intent IDs/text, complete unit universes, source digests,
and declared source spans. When changing any language source or labels, version
that language fixture rather than overwriting archived runs under an unchanged
fixture ID. For later comparisons, use a same-engine, same-language paired
baseline and preserve all eight per-query deltas.
