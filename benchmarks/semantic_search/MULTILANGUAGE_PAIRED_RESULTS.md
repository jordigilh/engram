# Paired multi-language qeval results

This report answers whether the Sense-inspired TypeScript lexical projection
and the Rust qualified-name/name-parts projection help on **each indexed source
language**, independently. Evaluation procedure and fixtures are described in
[`MULTILANGUAGE_PROTOCOL.md`](./MULTILANGUAGE_PROTOCOL.md) and
[`SYNTHETIC_EVALUATION.md`](./SYNTHETIC_EVALUATION.md). Each language has its
own source-authored truth, complete qrels, digest, and eight-query run. Scores
are never pooled across source languages.

## Same-engine paired results

The baseline and candidate for each engine/language pair used the same source
snapshot, qrels, model, CPU device, index/query flags, and source-unit `@10`
scorer. TypeScript baseline is the pre-Sense lexical projection; TypeScript
candidate is the Sense-inspired combined lexical field. Rust baseline is index
v2; Rust candidate is the language-neutral qualified-name/identifier-parts FTS
projection in index v3. Full raw/normalized runs, per-query metrics, source and
binary provenance, and implementation diff hashes are under
[`multilanguage-runs/2026-09-24-four-language-paired-v4/`](./multilanguage-runs/2026-09-24-four-language-paired-v4/).

| Source language | Engine change | Baseline nDCG / MRR / Recall / Precision | Candidate nDCG / MRR / Recall / Precision | Δ nDCG / MRR / Recall / Precision |
| --- | --- | --- | --- | --- |
| Go | TypeScript Sense lexical | 0.4023 / 0.7125 / 0.5708 / 0.2000 | 0.5888 / 0.7333 / 0.6771 / 0.2500 | +0.1865 / +0.0208 / +0.1063 / +0.0500 |
| Go | Rust lexical projection | 0.4710 / 0.6679 / 0.6208 / 0.2250 | 0.5942 / 0.7438 / 0.6771 / 0.2500 | +0.1231 / +0.0759 / +0.0563 / +0.0250 |
| Python | TypeScript Sense lexical | 0.5090 / 0.5130 / 0.6646 / 0.2375 | 0.5465 / 0.5479 / 0.6896 / 0.2500 | +0.0375 / +0.0349 / +0.0250 / +0.0125 |
| Python | Rust lexical projection | 0.5304 / 0.5438 / 0.6396 / 0.2250 | 0.5547 / 0.5688 / 0.6896 / 0.2500 | +0.0243 / +0.0250 / +0.0500 / +0.0250 |
| Rust | TypeScript Sense lexical | 0.4343 / 0.4340 / 0.5854 / 0.2125 | 0.4163 / 0.4556 / 0.5604 / 0.2000 | -0.0179 / +0.0215 / -0.0250 / -0.0125 |
| Rust | Rust lexical projection | 0.4301 / 0.4357 / 0.5854 / 0.2125 | 0.4627 / 0.4576 / 0.6479 / 0.2375 | +0.0326 / +0.0219 / +0.0625 / +0.0250 |
| TypeScript | TypeScript Sense lexical | 0.3419 / 0.4177 / 0.5542 / 0.2000 | 0.4268 / 0.5917 / 0.5500 / 0.2000 | +0.0849 / +0.1740 / -0.0042 / 0.0000 |
| TypeScript | Rust lexical projection | 0.3207 / 0.3240 / 0.5708 / 0.2000 | 0.4116 / 0.5746 / 0.5125 / 0.1875 | +0.0908 / +0.2506 / -0.0583 / -0.0125 |

**Go and Python:** both changes improve all four aggregate metrics in both
languages. This is the direct answer to whether the Go fixture gains carry over
to Python: they do in this independent Python lane.

**Rust and TypeScript source:** gains are mixed. The TypeScript projection
regresses nDCG, recall, and precision on Rust source; both projections regress
recall on TypeScript source, and the Rust projection also regresses precision.
Therefore, **neither change passes the four-language no-regression gate**.
Do not promote either as a universal lexical-retrieval improvement yet.

## Per-query diagnostics

The aggregate table does not replace the per-query evidence in
[`comparison.json`](./multilanguage-runs/2026-09-24-four-language-paired-v4/comparison.json):

- On Go, both candidate arms lose relevant top-10 evidence for
  `copy-selected-workflow-details`; for the TypeScript arm, the first relevant
  rank on `validate-workflow-parameters` also worsens.
- On Python, the TypeScript arm lowers nDCG for `interactive-selection-guard`;
  the Rust arm lowers nDCG for `record-discovered-membership` and
  `interactive-selection-guard` despite positive macro metrics.
- On Rust source, the TypeScript arm sharply lowers `record-discovered-membership`
  nDCG and recall.
- On TypeScript source, both arms lose recall on
  `validate-workflow-parameters`; the Rust arm's MRR/recall loss on that query
  is especially large.

These are small, source-authored synthetic suites: eight queries per language.
They establish language-specific regression signals, not real-repository
generalization or LLM answer correctness. Keep the four lanes separate and add
more queries per language before using them to claim broad support.
