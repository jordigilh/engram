# Corpus-specific search quality evaluation

This benchmark follows the classic IR test-collection approach: freeze the
corpus and queries, judge retrieved source spans for relevance, then compare
rankings against those judgments. CocoIndex, zvec-git, and Sense are candidate
generators, not relevance oracles. Backend agreement, file overlap, and raw
scores are not quality labels.

## Exploratory real-repository scope

The [synthetic fixture protocol](./SYNTHETIC_EVALUATION.md) is the repeatable
regression lane for zvec-grep retrieval changes. The Kubernaut run below is a
separate, narrow repository-specific investigation; its scores are not the
quality gate for the synthetic qeval work.

The initial corpus is the Kubernaut feature snapshot recorded in
[`kubernaut_fix-2442_8f3bc5a2_2026-09-23.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.json):
branch `fix/2442-workflow-discovery-membership`, commit
`8f3bc5a2d7da5262553a0919f9faecb83d61a09a`, selected-source digest
`1eb2a57192e0af48c80741d7d732a13028f93c2614d194e3ce278234cc859dd8`, and
1,072 production-Go files. The 7 base queries and 4 follow-ups are the prompts
to retain verbatim. The source-grounded target list in
[`kubernaut_fix-2442_8f3bc5a2_2026-09-23.relevance-draft.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.relevance-draft.json)
is an annotation draft only; do not calculate or publish quality metrics from
it until a human has reviewed it and judged the pooled candidates.

The first batch is narrow and workflow-discovery-heavy. Add representative
queries from other code areas before using it to make a general backend
decision. Keep queries grouped by intent so a win on exact identifiers cannot
hide a loss on conceptual or multi-step questions.

## Golden fixture lane

For fast regression checks, use a small fixture whose source is fully
controlled and whose relevant units are authored from that source before any
backend is run. The current fixture is
[`fixtures/go-workflow-discovery-v1`](./fixtures/go-workflow-discovery-v1/).
It is one of four independent source-language lanes; see
[`MULTILANGUAGE_PAIRED_RESULTS.md`](./MULTILANGUAGE_PAIRED_RESULTS.md) and its
[protocol](./MULTILANGUAGE_PROTOCOL.md) for Go, Python, Rust, and TypeScript
paired runs.
Its qrels are complete because the fixture's source truth defines the entire
unit universe; this is different from real-repository pooling, where qrels must
be adjudicated from candidate snippets. Fixture results are objective for that
fixture but must not be treated as evidence that a backend generalizes to all
repositories or additional languages. See [the protocol](./SYNTHETIC_EVALUATION.md)
for exact source, normalization, scoring, and rerun settings.

## Relevance unit and labels

Judge a source span or symbol implementation, not just a path. A query may have
multiple relevant spans. Use graded relevance:

| Grade | Meaning |
|---:|---|
| 0 | Does not help answer the query |
| 1 | Related context, but not useful evidence for the answer |
| 2 | Useful supporting evidence or one necessary step in a multi-step answer |
| 3 | Directly answers the query or is a primary implementation site |

Pool candidate spans from the same result cutoff for every backend. Blind the
judge to backend identity, inspect the actual snippet/source, and include
source-found answers that no backend returned. Record 0 for judged-irrelevant
pooled candidates; leave unjudged candidates explicitly unjudged, never silently
convert them to 0. Preserve the query, commit, source-scope digest, backend
version/configuration, result limit, and full ranked results with the
judgments.

The proposed symbols in the draft target file are hypotheses about likely
answers, not exhaustive qrels. In particular, queries about an undiscovered
workflow can refer either to the investigator's validator path or to the
interactive `select_workflow` gate. Split or clarify those intents if the
reviewer does not consider both answer surfaces relevant.

After judgments are complete, normalize each backend result to a stable
`unit_id` (source span or symbol) and save all three rankings in one run file.
The scorer in `scripts/evaluate_semantic_search.py` refuses draft qrels and any
returned result that is still unjudged; extend the pooled judgments first
rather than silently treating an unknown result as irrelevant. Example:

```sh
python3 scripts/evaluate_semantic_search.py \
  --qrels benchmarks/semantic_search/kubernaut_fix-2442_8f3bc5a2_2026-09-23.qrels-adjudicated.json \
  --runs benchmarks/semantic_search/kubernaut_fix-2442_8f3bc5a2_2026-09-23.normalized-runs.json \
  --k 10
```

## Diagnose the pipeline before tuning

Classify each miss at the earliest failing stage:

1. **Snapshot / scope / freshness** — the source revision or file is missing,
   stale, excluded, or over the configured size limit.
2. **Ingestion / extraction / representation** — the source is present but the
   relevant symbol/span was not extracted, was assigned the wrong entity kind,
   or its searchable text omitted useful code, names, documentation, or parent
   context.
3. **Candidate generation** — the relevant indexed unit is absent from the
   deep lexical and vector candidate lists.
4. **Query classification / ranking / fusion** — a route retrieves the relevant
   unit, but the selected query mode, weights, fusion, or reranker puts it below
   useful results.
5. **Presentation / context** — the right unit is ranked, but the returned
   snippet omits the explanation, relevant lines, or connected symbols needed
   to answer the task.

Do not call a low top-5 rank an ingestion defect unless index coverage is
checked. For structured-code questions, verify extraction and source mapping
first; then inspect the independent lexical/vector candidate ranks; only then
attribute the miss to fusion or reranking.

## Metrics

Report per query category and macro-average across queries:

- **Recall@50** for deep candidate coverage, before fusion/reranking.
- **nDCG@10** for graded top-of-list quality with multiple useful spans.
- **MRR@10** for exact-target questions with a clear primary answer.
- **Success@5** and task-level follow-up reads/calls as separate agent-workflow
  measures.

Do not compare raw model scores across backends. Keep index coverage, retrieval
relevance, snippet usefulness, latency, and task completion as separate
dimensions. Use a held-out query slice for tuning decisions.

## Candidate graph-aware ranking ablation

The Yahoo-versus-Google analogy suggests two complementary layers: classify
code entities and capture structure during ingestion, then rank them against
the query. It does not imply replacing text relevance with popularity.

Static, corpus-derived features are reasonable ingestion-time metadata:

- symbol kind and source module/domain;
- counts of **resolved** callers/callees, kept separate by edge type;
- graph/parser confidence and a graph-manifest or source-snapshot identifier;
- optionally, global PageRank computed over the same versioned graph.

Keep these as structured metadata or in the versioned codegraph sidecar, not as
synthetic text appended to the embedding input. A graph feature must be rebuilt
or incrementally updated with the same snapshot as the semantic index; otherwise
branch changes can leave stale authority scores attached to current code.

For query-time ranking, test personalized PageRank (PPR): seed a small set of
high-confidence hybrid results, walk only over selected typed edges, and use
the resulting candidate rank as a small secondary signal. The seed set and edge
direction can vary by query class (for example, caller-oriented questions versus
implementation lookup). Compare it against the existing grouped-query strategy
and simpler capped in-degree/global-PageRank features. Use rank fusion or a
small capped boost so graph centrality cannot override a strong direct lexical
or semantic match.

Global PageRank or raw reference count alone is a risky prior: popular utilities
and framework hubs can dominate even when a less-connected function is the
direct answer. The current name-based graph also contains unresolved and
ambiguous edges, so graph walks should start with high-confidence resolved
relations, with Serena/gopls spot checks for sampled edges. Keep graph-based
ranking as an offline ablation until qrels show a category-specific gain.

## Evidence already available

The branch run contains useful pipeline diagnostics, but not human relevance
judgments:

- For `discovered-state-flow-to-validation`, zvec's route diagnostics contain
  `SetDiscoveredWorkflowState` at rank 7, `DiscoveredWorkflowStateFromContext`
  at 9, `WithDiscoveredWorkflowState` at 10, and
  `NewDiscoveredWorkflowState` at 16. These symbols are in the indexed
  candidate universe; the top-10 result shape/ranking is the issue, not simple
  source omission. A state-focused supplemental query recovered the related
  state symbols within its own five-result group.
- On that same query, Sense v1.15.1 ranked `NewDiscoveredWorkflowState` #1,
  `DiscoveredWorkflowStateFromContext` #2,
  `Validator.SetDiscoveredWorkflowState` #4, and
  `WithDiscoveredWorkflowState` #5. Since zvec already has these symbols in its
  route candidate lists, this is evidence for a retrieval/organization gap on
  this query, not proof of an ingestion omission. The two systems return
  different result units, so the relevance judge must still inspect the source
  evidence.
- For `restrict-selection-to-discovered-workflows`, the zvec route diagnostics
  place `Validator.IsAllowed` at FTS rank 76 and vector rank 39 (hybrid rank 40
  at limit 100). An identifier-anchored supplemental query brought it to rank
  2 when kept as a separate group. This points to query-to-symbol matching and
  ranking/fusion, not a missing source file.
- For `record-listed-workflow-ids`, zvec returned `DiscoveredWorkflowState.Add`
  at rank 2; for `detected-labels-to-discovery-filters`, it returned
  `filtersFromSignal` at rank 2. Those are counterexamples to a blanket
  ingestion-failure diagnosis.

These examples make retrieval a demonstrated issue for some queries. They do
not rule out ingestion/extraction problems elsewhere; use per-target index
coverage checks to establish those separately.
