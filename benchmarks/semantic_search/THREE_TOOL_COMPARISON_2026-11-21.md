# Three-tool semantic retrieval comparison (2026-11-21)

zvec-grep (our fork) vs rgctl vs Sense on the five Engram synthetic
workflow-discovery lanes. Scope is retrieval quality and reproducibility only.
No call-graph claims are made here.

## Provenance

| Tool | Version / identity | Embedding | Notes |
|---|---|---|---|
| zvec-grep | fork `jordigilh/zvec-grep` @ `71eb185` (merged lanes), binary sha256 `686d9f70…a7d53e3` | `local/potion-code-16m-v2`, CPU | hybrid BM25+vector, unit-level k=10, 2 frozen-index repeats |
| rgctl | `rgctl/rgctl` v0.4.30, binary sha256 `f0879aad…eeecea9` | `vocab-accumulate-v2`, 256 dimensions, `vocab` embedder | `discover` + `semantic index` + `semantic query`; semantic index is **function symbols only** |
| Sense | `luuuc/sense` v1.15.1 (commit `a678631`), release `darwin_arm64` checksum-verified | bundled `all-MiniLM-L6-v2-ctx1` | `sense scan -embed`; default hybrid equals semantic on these queries (see ablation) |

All three ran on the same fixture bytes, same 8 queries per lane, k=10.
Models are tool-native; there is **no same-model claim** and no cross-tool
absolute ranking beyond the tables below.

## Corpus

| Lane | Files | Source bytes | Manifest units | Queries |
|---|---:|---:|---:|---:|
| go | 10 | 7,872 | 38 | 8 |
| python | 8 | 6,179 | 33 | 8 |
| rust | 14 | 8,073 | 39 | 8 |
| typescript | 8 | 5,670 | 33 | 8 |
| java | 9 | 13,149 | 40 | 8 |

Fixtures and adjudicated qrels live in
`benchmarks/semantic_search/fixtures/*-workflow-discovery-v1/`. The Java lane
was blind-audited before adjudication (see its README).

## Protocol

- Fixtures copied to scratch roots; originals never modified (sha256 pins preserved).
- Each tool indexed a fresh copy. rgctl: `discover`, then `semantic index`
  (default `vocab`, 256d), then `semantic query --limit 10 -f json`.
  Sense: `sense scan -embed`, then `sense search --json --limit 10`.
  zvec-grep used the lane runner (`run_qeval_multilanguage.py`, 2 repeats,
  frozen index).
- Hits map to manifest unit IDs:
  - rgctl: `(relative path, qualified_name)`, falling back to symbol suffixes.
  - Sense: `(relative path, symbol)` after stripping package/module prefixes.
- Unmatched hits are counted, not silently dropped.
- Determinism: each tool rebuilt its index in the **same root** and the
  rankings were compared. rgctl additionally showed a cross-root effect (see
  below).
- Scored with `benchmarks/quality-lanes/evaluate_semantic_search.py`, cutoff 10.

## Results

### nDCG@10

| Lane | zvec-grep | rgctl | Sense |
|---|---:|---:|---:|
| go | **0.5888** | 0.4555 | 0.5763 |
| python | **0.6387** | 0.4624 | 0.5258 |
| rust | **0.5977** | 0.3885 | 0.5312 |
| typescript | 0.4836 | 0.4938 | **0.5164** |
| java | 0.3250 | 0.3989 | **0.4227** |

### MRR@10

| Lane | zvec-grep | rgctl | Sense |
|---|---:|---:|---:|
| go | **0.7333** | 0.6115 | 0.7304 |
| python | **0.6375** | 0.5521 | 0.5938 |
| rust | 0.6458 | 0.3483 | **0.7812** |
| typescript | **0.7083** | 0.6146 | 0.6970 |
| java | 0.4792 | 0.4241 | **0.5458** |

### Recall@10

| Lane | zvec-grep | rgctl | Sense |
|---|---:|---:|---:|
| go | **0.6771** | 0.4229 | 0.6563 |
| python | **0.7757** | 0.5548 | 0.6110 |
| rust | **0.7423** | 0.6173 | 0.5525 |
| typescript | 0.5560 | 0.5775 | **0.6202** |
| java | **0.4702** | 0.4012 | 0.4515 |

### Precision@10

| Lane | zvec-grep | rgctl | Sense |
|---|---:|---:|---:|
| go | **0.2500** | 0.1625 | 0.2500 |
| python | **0.3250** | 0.2375 | 0.2750 |
| rust | **0.3000** | 0.2250 | 0.2500 |
| typescript | **0.2625** | 0.2500 | 0.2375 |
| java | 0.1750 | 0.1500 | **0.1875** |

## Determinism

- **zvec-grep:** deterministic by construction; the lane aborts if two repeats
  differ. All five lanes had identical repeats.
- **rgctl:** a rebuild in the same absolute root reproduced identical rankings.
  Two runs copied to **different absolute roots** reordered ties in Python
  (5 queries) and TypeScript (4 queries). Node IDs hash the absolute path, so
  a different checkout location is not reproducible even with identical source.
- **Sense:** same-root rebuild identical for Go, Python, Rust, TypeScript.
  Java is unstable even on the same index: six identical queries produced two
  orderings, swapping `Workflows.handleSelectWorkflow` and
  `WorkflowCatalog.listActions` at ranks 7–8. Keyword mode also showed one
  unstable lane (Rust), so the instability is mode/lane-specific rather than
  Java-only.

## Coverage and mapping

- **rgctl:** semantic index covers function symbols only. Class, record and
  field units in the qrels are structurally unreachable, which caps recall
  independently of retrieval quality. Unmatched hits: 0 (Go/Python/Rust),
  11 (TypeScript) and 9 (Java), all anonymous functions/lambdas with no
  manifest unit. Those hits still consume top-10 slots.
- **Sense:** indexes classes, methods and functions; 0 unmatched hits on all
  five lanes.
- **zvec-grep:** indexes manifest units, including containers. Its Java lane
  was measured at 0.3250 nDCG under the then-default model and fusion; see
  `JAVA_GAP_ROOT_CAUSE_2026-11-22.md` for the corrected analysis.

## Sense mode ablation

`--mode semantic` produced metrics identical to default hybrid on all five
lanes, so hybrid auto-routes these natural-language queries to the dense arm
and the keyword fallback does not change the measured ranking.
`--mode keyword` differs: nDCG go .5534, python .4885, rust .5481 (higher than
hybrid), typescript .4976, java .4135.

## Interpretation

- No tool dominates. zvec-grep wins nDCG on Go, Python and Rust; Sense wins
  TypeScript and Java; rgctl is never first.
- Sense beats rgctl on nDCG and MRR in all five lanes, and on recall in four;
  Rust recall is the exception (rgctl .617 vs Sense .553) while Sense's Rust
  MRR is much higher (.781 vs .348).
- rgctl's low recall is partly structural (function-only index), not purely a
  ranking result.
- zvec-grep's Java weakness is an engine-configuration effect (embedding
  model plus fusion weighting), not a source-documentation or fixture effect.
  **Corrected 2026-11-22**: see `JAVA_GAP_ROOT_CAUSE_2026-11-22.md`; the
  original "source-documentation" reading of issue #58 is withdrawn, and the
  fix landed in `zvec-grep#67`.
- The three tools use different embedding models. Treat these as per-tool
  evidence on one corpus, not a controlled model comparison.

## Threats to validity

- Eight queries per lane; no significance testing. Single frozen snapshot per
  language.
- Tiny corpora (5.7–13.1 KB). They exercise ranking, not scale.
- Mapping rules are ours; unmatched counts are reported but some legitimate
  tool outputs (lambdas) cannot map to manifest units.
- Sense ran on bundled ONNX; rgctl on its compiled vocab table; zvec-grep on
  potion-code. Model quality is not isolated.

## Reproduction

- zvec-grep: `python3 benchmarks/quality-lanes/run_qeval_multilanguage.py
  --fixture <fixture> --binary rust/target/release/zg --work-root <dir>
  --output <dir> --repeats 2`.
- rgctl: `rgctl discover <root> -f json`; `rgctl semantic index -r <root>`;
  `rgctl semantic query -r <root> --limit 10 -f json "<query>"`.
- Sense: `sense scan -dir <root> -embed -quiet`;
  `sense search "<query>" --limit 10 --json [--mode hybrid|semantic|keyword]`.
- Scorer: `benchmarks/quality-lanes/evaluate_semantic_search.py`, cutoff 10.

## Artifacts

- zvec-grep lane floors: `benchmarks/quality-lanes/baselines-multilanguage.json`.
- rgctl runner and summary (ephemeral):
  `…/opencode/rgctl-qeval/{run_rgctl_qeval.py,summary.json,run1/}`.
- Sense runner and summaries (ephemeral):
  `…/opencode/sense-qeval/{run_sense_qeval.py,summary-semantic.json,summary-keyword.json,runs/}`.

Ephemeral paths are under the session temp dir and may be removed; the metric
values above are the durable record.

## Conclusion

On the five adjudicated synthetic lanes, each tool is best on a different
subset. zvec-grep leads nDCG on three languages and recall on four; Sense leads
TypeScript and Java nDCG and is the stronger second-stage ranker (higher MRR on
Rust and Java); rgctl is behind both on this protocol, with part of the gap
attributable to its function-only semantic index. Determinism differs
qualitatively: zvec-grep is repeat-stable, rgctl is same-path-stable but
path-dependent, and Sense has a same-index tie instability in at least one
mode/lane. Models are not controlled, so none of this supports a general
"tool X is better" claim.
