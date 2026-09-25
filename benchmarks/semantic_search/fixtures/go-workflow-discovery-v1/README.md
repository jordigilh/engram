# Synthetic Go Workflow Discovery Fixture

This fixture is a small, source-controlled regression corpus for semantic code
search. It contains production-shaped Go symbols, realistic distractors, and
eight query intents covering exact lookup, semantic behavior, data flow, retry
state, interactive guards, validation, failure modes, and presentation.

The relevance truth in `truth.json` is authored from the fixture source. It is
not inferred from CocoIndex, zvec-git, Sense, or agreement between backends.
Run `scripts/build_synthetic_qrels.py` to expand the source-authored targets to
the complete unit universe and produce `qrels.json`.

The unit IDs are qualified source symbols such as
`internal/selection/validator.go::Validator.IsAllowed`. This makes the fixture
stable across chunk sizes and result formatting while keeping the source anchor
auditable.

See [qeval results](./QEVAL_RESULTS.md) for measured runs, decisions, and the
zvec-grep issue tracking retrieval experiments. The complete construction,
replay, scoring, and provenance procedure is the
[synthetic evaluation protocol](../../SYNTHETIC_EVALUATION.md).
