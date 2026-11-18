# Code-Search Tool Scorecard

**Status:** Proposed standard — v1, 2026-11-16

**Tracking:** [issue #189](https://github.com/jordigilh/engram/issues/189)

**Motivation:** semantic code-search and call-graph tools (zvec-grep,
rgctl, CocoIndex, and others) generally do not publish evaluation results.
Where we measured them ourselves on identical frozen fixtures against
system-independent adjudicated qrels, scores differed materially between
systems and were lower than each tool's implied quality. Structural
(call-graph) claims are even less standardized: name-based extractors can
report edges a compiler refutes, and a precision figure without a recall
figure is gameable by asserting fewer edges.

This document defines the minimum public measurement set we hold any
code-search tool to before trusting its retrieval or structural claims.

## Panel 1 — Retrieval quality

Measures the "semantic search" claim: given a natural-language query, does
the tool surface relevant source units, ranked well?

Required public artifacts:

- **Corpus**: frozen fixture or benchmark corpus, content-hashed
  (SHA-256 of source snapshot). Public benchmarks such as CoIR tasks are
  acceptable; private fixtures are acceptable only if queries, qrels, and
  source hashes are published.
- **Query set and qrels**: adjudicated relevance judgments per query,
  system-independent (never produced by the tool under test or its own
  models). The CoSQA lesson applies: an estimated ~51% of CoSQA labels are
  mismatched pairs, so label provenance and adjudication method must be
  stated.
- **Configuration**: embedding model and version (cross-model comparisons
  are invalid), raw candidate limit, feature flags, engine commit.
- **Relevance threshold**: the minimum qrel grade that counts as relevant
  for MRR@10, Recall@10, and Precision@10. Each report states the
  threshold; the same ranking scores differently under different cutoffs.

Required metrics, per language:

| Metric | Definition |
| --- | --- |
| nDCG@10 | ranking quality with graded relevance |
| MRR@10 | first-relevant-result quality |
| Recall@10 | retrieval breadth at the reported limit |
| Precision@10 | retrieval exactness at the reported limit |

Publication rules:

- All four metrics together; a single favorable metric is not a result.
- The evaluation harness or its scoring script is public or reproducible.
- Repetitions and determinism stated (run-to-run metric variance).

## Panel 2 — Structural accuracy

Measures the "call graph" / "code graph" claim: are the reported edges
real?

Required public artifacts:

- **Oracle**: the language's own compiler toolchain, on pinned public
  repositories. Go: `golang.org/x/tools` RTA over the type-checked
  program. RTA is an agreement baseline, not a proof of actual calls: it
  can link a dynamic call site to every compatible address-taken
  function, and it omits reflection edges. TypeScript: the `tsc`
  checker. C#: Roslyn. Java: javac/OPAL.
  Python/Ruby/PHP have no compiler oracle (calls resolve at runtime);
  dynamic baselines (fixed entry points + coverage-guided corpora, per
  OPAL ISSTA'24) are the substitute and must be labeled as such.
- **Edge export**: the tool's emitted edges in a documented format, with
  the tool's own confidence/resolution status preserved (e.g. resolved vs
  pending).
- **Reproducibility**: tool commit, oracle toolchain versions, export
  command.

Required metrics, per language and per repository:

| Metric | Definition |
| --- | --- |
| Edge precision | share of asserted edges the oracle's edge set includes |
| Edge recall | share of oracle edges the tool asserts |
| F1 | harmonic mean |
| Direction flips | edges asserting caller/callee reversed |
| Resolution split | asserted vs pending/unresolved counts |

Publication rules:

- **Precision must be published with recall.** Precision alone is
  gameable by asserting only trivially-safe edges (a tool drawing 17% of
  real calls can still score ~0.99 precision).
- Precision is computed over *asserted* edges; if the tool exports
  lower-confidence edges, both the asserted-only and all-edges precision
  are reported, and consumers are told which to use.
- Recall is measured against the oracle edge set, not against all true
  edges; it is not a bound on true recall, because the oracle can both
  over- and under-approximate (RTA dispatch fan-out vs. omitted
  reflection edges). Correct edges the oracle does not include count
  against precision, not recall. State the oracle's coverage limits with
  each recall value.
- Determinism: two fresh extractions must produce identical edge sets, or
  the variance is reported.

## Worked example (2026-11-16 shootout)

Frozen Go fixture `go-workflow-discovery-v1`, oracle
`go/packages`+SSA+RTA (18 edges, schema `go-graph-truth-v1`), clean
upstream builds:

| Tool | Asserted edges | Precision | Recall | Flips | Deterministic |
| --- | ---: | ---: | ---: | ---: | --- |
| zvec-grep codegraph-v2 | 18 resolved (+25 pending, external) | 94.4% | 94.4% | 0 | yes |
| rgctl v0.4.27 (`4d4d1b3`) | 18 | 94.4% | 94.4% | 0 | yes |
| e3-qeval-callscan-v1 (synthetic) | 61 | 26.2% | 88.9% | 0 | n/a |

Single shared failure: same-name collision
`tools.ListWorkflows` → `catalog.ListWorkflows` resolves as a self-loop
instead of `discovery.WorkflowCatalog.ListWorkflows` — receiver-qualified
method disambiguation. Naive consumption of zvec's full 43-edge export
(ignoring the resolved/pending split) yields 39.5% precision.

Panel 1 for the same program (same fixtures, adjudicated qrels, raw limit
10, source-unit normalization) is recorded in issue #189: rgctl
0.4556/0.6115/0.4229/0.1625 vs zvec-grep TypeScript engine
0.4326/0.5556/0.6125/0.2125 (nDCG/MRR/Recall/Precision, Go lane) — each
tool wins different metrics, which is why all four are mandatory.

## Known limits

- Fixture-scale measurements (tens of edges) distinguish coarse failure
  modes, not percentage-point ranking between close tools; repository-
  scale oracles are needed for that.
- No oracle exists for dynamic-dispatch resolution in interpreted
  languages; Panel 2 claims there are inherently bounded.
- Retrieval qrels age: adjudicated relevance reflects the adjudicators;
  re-adjudication is required when queries or corpora change.
