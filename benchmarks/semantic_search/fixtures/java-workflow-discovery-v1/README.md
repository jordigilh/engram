# Java Workflow Discovery Fixture (v1)

Controlled, source-authored retrieval fixture for a synthetic workflow
catalog/discovery/selection/execution service. It mirrors the domain and
query intents of the four existing `{go,python,rust,typescript}-workflow-discovery-v1`
lanes so retrieval results are comparable across languages.

- 9 production `.java` files, 13,149 bytes total (1.0-2.3 kB each)
- 40 manifest units (10 classes, 4 records, 23 methods, 2 constructors,
  1 static field)
- 8 queries, at least 2 graded answer units each, plus co-located near-miss decoys
- 8 queries x 40 units of judgments in `qrels.json` (`judgment_status: "adjudicated"`)

## Adjudication

The draft labels were audited blind: an independent pass received the 9 source
files and the 8 query texts with `qrels.json` and `truth.json` withheld, graded
every unit from source, and only then compared. Threshold (grade >= 2)
agreement was 88.4%; exact-grade agreement 72.8%, quadratic weighted kappa
0.648. Six disagreements were reconciled in the draft:

- `reject-undiscovered-workflow`: `isWorkflowInDiscoveryResult` 1 -> 3 and
  `handleSelectWorkflow` 0 -> 2 (both rejection lanes are now credited, matching
  the corrected Rust/TypeScript fixtures); the `WorkflowState` class 0 -> 2
  because its contract states the never-discovered policy.
- `empty-discovery-fails-closed`: `handleSelectWorkflow` 0 -> 2 for the same
  reason.
- `preserve-membership-on-retry`: `stateFromContext` 2 -> 1; no retry path in
  this fixture calls it.
- `validate-workflow-parameters`: `MAX_PARAMETERS` 0 -> 2; `validateParameters`
  enforces that bound before execution.

The audit's blanket proposal to promote every container class and record to
grade 2 was not applied; only units whose own contract states the queried
behavior are credited. Source files and the snapshot digest are unchanged by
adjudication.

Only the Java standard library is used. No framework, build tool, or external
dependency is required.

## Layout

| Path | Units | Role |
| --- | --- | --- |
| `catalog/Workflow.java` | 1 | Immutable workflow record |
| `catalog/WorkflowCatalog.java` | 7 | Catalog listing, identity lookup, label normalization, near-miss wrapper |
| `discovery/WorkflowState.java` | 4 | Shared discovered-ID set and context recovery |
| `discovery/DiscoveryFilters.java` | 5 | Signal-label to filter conversion and label filtering |
| `selection/WorkflowValidator.java` | 6 | Catalog + discovery membership policy and rejection error |
| `selection/RetryPolicy.java` | 3 | Self-correction on top of the same validator state |
| `execution/WorkflowRunner.java` | 4 | Parameter validation and execution entry point |
| `tools/Workflows.java` | 8 | Discovery/selection facade, recommendation set, result assembly |
| `metrics/MetricNames.java` | 2 | Metric-name validation |

## Near-miss decoys

Two same-name helper types exist on purpose, so a query must be answered by the
right package:

- `catalog.Normalize.labelValue` (catalog label matching: strip and lowercase)
- `discovery.Normalize.term` (signal term conversion: strip and lowercase)

Both normalize text but serve different layers. Additionally,
`catalog.WorkflowCatalog.listActions` is a near-miss wrapper that forwards an
already-built filter map and never converts signal labels; it is graded 0 for
the label-conversion query.

## Provenance

- Source: synthetic, authored for this fixture. No upstream repository,
  commit, or third-party code is involved.
- Repository: `synthetic/java-workflow-discovery`
- Revision: `fixture-v1`
- Language level: Java 21 (`javac --release 21`); authoring toolchain:
  OpenJDK 27.0.1 (`javac 27.0.1.0.3`).
- Adjudication: Engram commit `ca6231c` (blind audit; see above).
- Source snapshot: see `source.snapshot_sha256` in `manifest.json` and
  `qrels.json`.

## Vendored copy and regeneration

This directory is a vendored copy of the fixture's source of truth in the
Engram repository:

```
jordigilh/engram
  benchmarks/semantic_search/fixtures/java-workflow-discovery-v1/        (fixture)
  benchmarks/semantic_search/fixtures/_tools/java-manifest/ManifestGen.java
  benchmarks/semantic_search/fixtures/_tools/java-manifest/build_draft_qrels.py
  scripts/verify_java_workflow_fixture.py
```

The generator and verifier are committed there, not in this lane. Run the
commands below from an Engram checkout. This lane only consumes the vendored
`manifest.json`, `truth.json` and `qrels.json`.

### Manifest

`manifest.json` is generated, never hand-written:

```sh
# from the Engram repository root
javac -d /tmp/java-manifest \
  benchmarks/semantic_search/fixtures/_tools/java-manifest/ManifestGen.java
java -cp /tmp/java-manifest ManifestGen \
  benchmarks/semantic_search/fixtures/java-workflow-discovery-v1
```

The generator parses every included source with javac, emits one unit per
top-level or nested type and per method, constructor, and static field of
interest, maps javac positions to 1-based inclusive lines, and refuses to write
the manifest when a span does not begin with its `declaration_anchor`.

Unit IDs are `<relative path>::<Type>` or `<relative path>::<Type>.<member>`,
with constructors written as `Type.constructor` and nested records as
`Outer.Inner`. Examples:

```
catalog/WorkflowCatalog.java::WorkflowCatalog.listWorkflows
discovery/WorkflowState.java::WorkflowState.add
tools/Workflows.java::Workflows.DiscoveryResult
```

Unit fields: `unit_id`, `path`, `symbol`, `kind`
(`class|record|interface|method|constructor|field`), `start_line`, `end_line`,
`declaration_anchor` (first non-annotation line of the declaration, trimmed).
Record components and non-static instance fields are not units; only static
fields of interest are.

### Snapshot recipe (`source.snapshot_sha256`)

1. Select files by `source.include` (`**/*.java`) minus `source.exclude`
   (`_tools/**`), then sort paths lexicographically.
2. For each path, compute the SHA-256 of the raw file bytes.
3. Build one line per file: `<relative path>` + TAB + `<file sha256 hex>`.
4. Join the lines with LF (no trailing newline) and compute SHA-256 over the
   UTF-8 bytes of that joined text.

`source.files`, `source.bytes`, and `source.snapshot_sha256` are derived from
that selection. The same recipe is implemented in `ManifestGen.java`,
`build_draft_qrels.py`, and `scripts/verify_java_workflow_fixture.py`.

### Truth and qrels

`truth.json` is authored from the fixture source, not inferred from any search
backend. `qrels.json` expands it to a judgment for every manifest unit in every
query (8 x 40 = 320 rows, grade 0 by default). The builder writes a **draft**;
do not overwrite the adjudicated judgments with a regenerated draft unless the
reconciliation is repeated.

```sh
# from the Engram repository root
python3 benchmarks/semantic_search/fixtures/_tools/java-manifest/build_draft_qrels.py \
  --fixture benchmarks/semantic_search/fixtures/java-workflow-discovery-v1 \
  --output benchmarks/semantic_search/fixtures/java-workflow-discovery-v1/qrels.json
```

## Status: adjudicated and registered

`qrels.json` declares `"judgment_status": "adjudicated"` after the blind audit
and reconciliation described above. The fixture is registered as the fifth
language in the fork's `retrieval-quality-multilanguage` lane.

## Verification

From the Engram repository root:

```sh
python3 scripts/verify_java_workflow_fixture.py
```

The verifier compiles the fixture, checks file/size/count bounds, resolves every
span against its anchor, recomputes the snapshot, confirms truth and qrels agree
grade-for-grade with full per-query coverage, checks query answer/decoy shape,
confirms the deliberate decoys, asserts the adjudicated status, and reports
whether `git status --porcelain` attributes only the fixture and tooling paths
to this work.
