# Code Retrieval Representation Lessons

**Status:** Follow-up record — v1, 2026-11-16

**Tracking:** engram issue [#191](https://github.com/jordigilh/engram/issues/191)
(rgctl program), zvec-grep issue [#8](https://github.com/jordigilh/zvec-grep/issues/8)
(Code IR retake)

**Scope:** What the rgctl-informed enhancement program (E0–E6, the
combined rerank arm, and the graph-truth measurements) established about
*representations* in code retrieval — and what that means for any future
normalized IR work.

## The central lesson

Two architectures for using normalized code structure in retrieval were
tested. One is dead; the other is validated:

| Approach | Tested by | Verdict |
| --- | --- | --- |
| Normalized fields **replace** the retrieval corpus | E1 (`all`/`body`/`symbol` representations) | **Dead.** `all` was zero-delta everywhere; `body`/`symbol` mixed by language with query-level regressions. E2 (token sketches as representation) failed in Rust×Rust and TypeScript lanes. |
| Normalized fields **augment** retrieval as bounded, per-field rerank signals on top of unchanged hybrid retrieval | Combined Go-lane arm (`0.80·hybrid + 0.10·name_overlap + 0.05·token_sketch + 0.05·graph_signal`) | **Validated.** With compiler-confirmed graph edges: +0.045 nDCG@10 / +0.146 MRR@10 / zero recall loss vs baseline (Go lane). |

`name_overlap`, `token_sketch`, and `graph_signal` are all normalized
per-field extractions. None replaced the corpus. The fragment-based hybrid
baseline is hard to beat precisely because it already contains implicit
structure (lexical symbol names, `identifierParts`, `preferSymbol`); what
it lacks is *explicit, bounded, per-field evidence* at the rerank stage.

## Why cross-language universality was the wrong expectation

Every measured cross-language result in the program was language-dependent:

- rgctl's wins over zvec-grep concentrated in Go (nDCG/MRR) while losing
  recall in every language;
- E2 failed the universal gate via Rust×Rust and TypeScript regressions;
- E3 failed three of four language lanes;
- the combined arm passed only because of per-language gating (Go on,
  others byte-identical with the feature off).

What is language-independent is the *substrate* (unit, kind, receiver,
signature, spans, provenance, facts). What is language-specific is *what
makes something findable*: Go's receiver-qualified names, Rust's trait
implementations, Python's duck-typed attribute access. The correct
architecture is therefore one normalized substrate carrying **per-language
signal profiles** — the same field set, with rerank weights conditional on
language (and potentially query class).

## Structural truth is the highest-value signal — and it is measurable

The compiler-truth measurements (2026-11-16) established the quality
ladder for graph edges:

| Edge producer | Precision | Recall |
| --- | ---: | ---: |
| Synthetic name-mention scan (e3-qeval-callscan-v1) | 26.2% | 88.9% |
| zvec codegraph-v2 (asserted/resolved edges) | 94.4% | 94.4% |
| rgctl v0.4.27 (entity-to-entity edges) | 94.4% | 94.4% |
| Compiler (go/packages + SSA + RTA) | 100% (by construction) | static ceiling |

Filtering the synthetic producer's edges to the 16 compiler-confirmed ones
improved every ranking metric and eliminated the arm's only regression
(recall −0.0104 → 0.0000). The 45 refuted edges were type mentions
promoted to call edges — pure noise with zero MRR contribution. Both real
tools shared one failure: a same-name collision resolving to a self-loop
(`tools.ListWorkflows` → `catalog.ListWorkflows`), fixable with
receiver-type qualification; compiler-backed extraction (e.g. scip-go)
removes the class entirely.

Consequence: graph signals must be validated against compiler truth before
entering ranking, and the resolved/pending (or equivalent confidence) split
must be respected by every consumer — naive consumption of zvec's full edge
export measured 39.5% precision.

## Confidence calibration for a Code IR retake

- Compiler-exact structure improving ranking where a compiler oracle
  exists (Go, TS, Java/C#): high (~75–80%). n=1 language and n=1 fixture
  at time of writing; the locked holdout fixture `go-inventory-audit-v1`
  is the generalization gate.
- Normalized representations improving retrieval as corpus replacement:
  low (~35–40%) — direct negative evidence (E1/E2).
- Normalized fields as bounded rerank signals: validated on Go, untested
  per-language elsewhere.
- Cross-language engineering benefits (determinism, incremental re-embed,
  provenance, evaluation variance): high (~85%), but these do not move
  ranking metrics.

## Retake methodology (agreed 2026-11-16)

Sequenced **after** the rgctl enhancement program closes. The inversion
versus the September 2025 attempt: measurement gates bind every phase,
rather than being optional and deferred.

1. Expectations first: each phase states a falsifiable claim with numbers
   (e.g. "compiler-resolved edges ≥95% precision on all four lanes";
   "dirty-worktree fallback zero ranking delta"; "one-symbol edit re-embeds
   one unit").
2. Yardsticks before implementation: compiler truth per language (Go tool
   exists; Python/Rust/TS oracle tooling to build or explicitly bounded),
   frozen fixtures with pre-measured baselines, adjudicated qrels.
3. Per-goal frozen baselines of unmodified current code.
4. Isolated, independently killable issues, in order:
   Go compiler-backed edge provider → normalized unit schema with
   provenance → per-language frontends (each oracle-gated) → vector
   projections over normalized units (E1-style gates, per language) →
   IR as publication authority last.
5. 90% confidence rule at every gate; default-off throughout.

The September design documents and the SCIP interoperability analysis
(position-encoding normalization, snapshot attestation, `type_resolved`
provenance gating) are retained as spec input; only the build-first
sequencing is retired.

## Related records

- Enhancement evaluation record: `docs/RGCTL_SEMANTIC_SEARCH_ENHANCEMENTS.md`
  (branch `docs/rgctl-semantic-enhancements`), engram issue #191.
- Scorecard standard: `docs/CODE_SEARCH_TOOL_SCORECARD.md`.
- Graph-truth artifacts: `go-graph-truth-output/`, `graph-precision-shootout-output/`
  (session temp; hashes and method in their REPORT.md files).
- Holdout fixture: `go-inventory-audit-v1` (locked 2026-11-16), baseline
  nDCG 0.5946 / MRR 0.7875 / Recall 0.725 / Precision 0.275.
