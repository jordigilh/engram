# Synthetic semantic-search qeval protocol

This is the reproducible **retrieval** evaluation used for zvec-grep experiments
tracked in [zvec-grep issue #6](https://github.com/jordigilh/zvec-grep/issues/6).
The fixed example is [`go-workflow-discovery-v1`](./fixtures/go-workflow-discovery-v1/),
with its [measured runs](./fixtures/go-workflow-discovery-v1/QEVAL_RESULTS.md).
[`QUALITY_EVALUATION.md`](./QUALITY_EVALUATION.md) describes the separate
real-repository judging process. The Go fixture is an evaluation corpus, **not**
a Go-only retrieval-design requirement.

## What is frozen

| Input | Source of truth | Contract |
| --- | --- | --- |
| Source | `fixtures/go-workflow-discovery-v1/internal/**/*.go` | Ten production-shaped Go files, with relevant and distracting declarations. No tests, generated code, or other project files in an indexed source root. |
| Unit universe | [`manifest.json`](./fixtures/go-workflow-discovery-v1/manifest.json) | 38 qualified `path::symbol` IDs, one per source declaration. |
| Questions and positive judgments | [`truth.json`](./fixtures/go-workflow-discovery-v1/truth.json) | Eight fixed query strings/categories and source-authored grades/rationales, decided from the source rather than a backend's answers. Do not rewrite queries or labels in response to a run. |
| Complete judgments | [`qrels.json`](./fixtures/go-workflow-discovery-v1/qrels.json) | The builder adds grade 0 for every unlisted manifest unit for every query; grades range from 0 to 3. |
| Source inventory | `qrels.json` → `source` | Ten `.go` files, 7,872 bytes, SHA-256 `94183663d9e5f81b753dde7c460428c2414c46867b936b5cc42f9827dab0827f`. |

The digest is SHA-256 of each selected Go file in sorted relative-path order,
with `path UTF-8`, NUL, raw file bytes, NUL per file. `_test.go` is excluded.
`go.mod` is present for the fixture's module but is **not** part of the selected
source or digest. Changing source, unit IDs, query text, or truth creates a new
fixture version and new qrels; preserve v1 for comparisons against its saved runs.
Grades mean: 0 unrelated, 1 related but insufficient, 2 useful supporting
evidence, 3 direct answer/primary implementation. Grades 2–3 count as relevant
for binary metrics. `source_truth=true` means that judgments were authored from
the controlled source, rather than pooled from any search engine's results.

## Re-score an archived run (no backends or database required)

From the Engram checkout root, first verify the frozen source and full qrels:

```sh
python3 -m pytest -q tests/test_synthetic_semantic_search.py
python3 scripts/evaluate_semantic_search.py \
  --qrels benchmarks/semantic_search/fixtures/go-workflow-discovery-v1/qrels.json \
  --runs benchmarks/semantic_search/fixtures/go-workflow-discovery-v1/replays/2026-09-24-zvec-lexical-reverted/normalized-runs.json \
  --k 10
```

If `pytest` is installed only in Engram's virtualenv, use its Python executable
for both commands. The test checks that rebuilding qrels from `truth.json`,
`manifest.json`, and the current source yields identical JSON data,
including the source digest and all 38 judgments per query. The scorer rejects
incomplete/draft qrels, unknown returned IDs, duplicate IDs per query, and
missing queries. The saved `normalized-runs.json` plus the frozen qrels are
sufficient to reproduce the **numbers** without model inference. For the Rust
paired graph/no-graph experiment, both runs are in the same
[`normalized-runs.json`](./fixtures/go-workflow-discovery-v1/replays/2026-09-24-zvec-proposal2-relationship/normalized-runs.json).

## Make a fresh three-backend replay

The commands below describe the current adapter in
[`scripts/replay_synthetic_semantic_search.py`](../../scripts/replay_synthetic_semantic_search.py).
Use an Engram environment with dependencies from `pyproject.toml` (including
CocoIndex), a disposable PostgreSQL/pgvector table, a zvec-grep binary built at
the revision under test, Sense **v1.15.1**, and the
`local/potion-code-16m-v2` model for the zvec arm. Record the exact binary,
model, and dependency revisions as described under **Provenance**; their names
alone do not pin their bytes. The CocoIndex materializer uses the Engram
`sentence-transformers/all-MiniLM-L6-v2` embedding and its 1,000-character,
300-overlap AST-aware code splitter. Sense supplies its own embeddings.

Stage **three independent, fresh** roots containing only the same ten Go source
files in the same relative paths; keep the search indexes and state outside the
Engram fixture. For example, with `PY` pointing to Engram's Python environment,
`ZG_BIN` and `SENSE_BIN` absolute binary paths, and `MODEL_CACHE` an absolute
local model-cache path:

```sh
: "${PY:?set PY to Engram's Python executable}"
: "${ZG_BIN:?set ZG_BIN to an absolute zvec-grep binary path}"
: "${SENSE_BIN:?set SENSE_BIN to an absolute Sense v1.15.1 binary path}"
: "${MODEL_CACHE:?set MODEL_CACHE to the downloaded model cache path}"
: "${COCOINDEX_PG_URL:?set COCOINDEX_PG_URL to the evaluation pgvector database}"
FIXTURE="$PWD/benchmarks/semantic_search/fixtures/go-workflow-discovery-v1"
WORK="$(mktemp -d)"
COCO_ROOT="$WORK/coco"
ZVEC_ROOT="$WORK/zvec"
SENSE_ROOT="$WORK/sense"
TABLE=synthetic_go_workflow_discovery_v1_example
for root in "$COCO_ROOT" "$ZVEC_ROOT" "$SENSE_ROOT"; do
  mkdir "$root"
  rsync -a --include='*/' --exclude='*_test.go' --include='*.go' --exclude='*' "$FIXTURE/" "$root/"
  diff -qr "$FIXTURE/internal" "$root/internal"
done

COCOINDEX_DB="$WORK/cocoindex-state.db" COCOINDEX_PG_URL="$COCOINDEX_PG_URL" \
  PYTHONPATH=src "$PY" -m engram.code_overlay_cocoindex \
  --root "$COCO_ROOT" --repo-tag synthetic-go-workflow-discovery-v1 \
  --table "$TABLE"

"$ZG_BIN" --index "$ZVEC_ROOT" --mode direct \
  --embedding local/potion-code-16m-v2 --model-cache "$MODEL_CACHE" --device cpu
( cd "$SENSE_ROOT" && "$SENSE_BIN" scan --embed )

PYTHONPATH=src:. "$PY" scripts/replay_synthetic_semantic_search.py \
  --fixture "$FIXTURE" --output-dir "$WORK/replay" \
  --coco-pg-url "$COCOINDEX_PG_URL" \
  --coco-table "$TABLE" \
  --coco-repo-tag synthetic-go-workflow-discovery-v1 \
  --zvec-binary "$ZG_BIN" --zvec-root "$ZVEC_ROOT" --model-cache "$MODEL_CACHE" \
  --sense-binary "$SENSE_BIN" --sense-root "$SENSE_ROOT"

python3 scripts/evaluate_semantic_search.py \
  --qrels "$FIXTURE/qrels.json" --runs "$WORK/replay/normalized-runs.json" --k 10
```

Choose a **new, safe SQL table name per run**, using only lowercase letters,
digits and underscores; replace the example `TABLE` value. Never point the
materializer at a shared production search table.
Before comparing scores, confirm each backend indexed the same ten Go paths and
the staged source hashes match the qrels digest. Do not use an implicit search-
time index build: the replay uses `--refresh off` for zvec and assumes that
indexing, including Sense embeddings, has completed. CocoIndex's `branch="main"`
parameter here filters release-tagged rows; the unique table and source digest,
not that branch argument, establish the fixture snapshot.

The adapter issues **exactly the eight `truth.json` queries** in order, with
`limit=10`: CocoIndex `search_code(mode="hybrid")`; zvec direct hybrid with
`--refresh off --compact --preview full` (and CPU when `--model-cache` is
provided); Sense `search --language go --json`. It saves full backend responses
as `raw-runs.json` and source-unit rankings as `normalized-runs.json`.
It runs **all three backends**; a zvec-only ablation is not an option in this
script. For a zvec-only ablation, use `scripts/replay_synthetic_zvec.py` with
the same fixture, a separate freshly built zvec root, and an explicit backend
label. It validates the root's source digest before querying and preserves raw
responses, normalized units, and scored metrics in a new output directory:

```sh
python3 -m scripts.replay_synthetic_zvec \
  --fixture "$FIXTURE" --root "$ZVEC_ROOT" --binary "$ZG_BIN" \
  --model-cache "$MODEL_CACHE" --backend rust-lexical-projection \
  --output-dir "$WORK/zvec-only-replay"
```

Keep a same-implementation control run with a different backend label; scoring
against the archived Rust control is only comparable when its source, model,
search path, and query settings have been checked against the current run.

## Result mapping and score contract

[`replay_synthetic_semantic_search.py`](../../scripts/replay_synthetic_semantic_search.py)
maps zvec source line ranges and CocoIndex code snippets to **every** manifest
symbol whose source span intersects the result; Sense's path and symbol name
must map to exactly one unit. CocoIndex snippets are located against the frozen
fixture source; an unmappable result fails replay rather than becoming a
file-level hit. Units are deduplicated at their first occurrence and each
retains `backend_rank` (the rank of the original chunk/symbol). A single chunk
can therefore yield several normalized units with the *same* backend rank.

[`evaluate_semantic_search.py`](../../scripts/evaluate_semantic_search.py)
scores the first **ten normalized units**, not the first ten backend results or
ten distinct files. For each query, DCG uses `(2^grade - 1) / log2(rank + 1)`;
nDCG divides by the ideal ordering of the adjudicated grades. MRR is the
reciprocal rank of the first grade-2-or-3 unit (zero if absent). Recall is the
number of relevant units in the first ten divided by **all** relevant units for
that query. Precision divides that retrieved count by **10**, even if fewer
than ten units are returned. Metrics are macro-averaged over eight queries;
category metrics are averaged separately. Report per-query results too: an
aggregate gain can hide a failure-mode regression.

The score is *retrieval of source units*, not answer correctness or snippet
usefulness. Chunk-to-unit overlap can favor backends with broader chunks;
`@10` is not yet a group-aware cutoff. File overlap, model scores across
backends, and the Kubernaut real-repo scores are not substitutes for these
qevals. `Recall@50` requires a separate deeper retrieval run—ten-result raw
replays cannot establish it. Eight queries from one Go fixture cannot show
generalization; use another language/held-out queries before claiming that.

## Provenance and comparisons

For **every new run**, preserve the two JSON artifacts above, the scorer's
output, and a `run-manifest.json` beside them. The replay script does **not**
currently write all environment/version fields for you. Record at least:

- Fixture ID, source SHA-256, qrels/query and manifest hashes, result limit,
  raw-hit cutoff, and normalization/scorer contract (`source-unit@10`).
- Engram commit plus any dirty diff identifier; zvec-grep implementation
  (TypeScript or Rust), commit plus any dirty diff, binary hash and index version;
  Sense version/commit and binary hash; CocoIndex version, Engram materializer
  revision, chunk settings and model revision/hash.
- zvec embedding model ID **and downloaded model snapshot/hash**, CPU/device,
  index flags and root scope; Sense `scan --embed` settings; CocoIndex table name,
  state DB identity and PostgreSQL/pgvector versions. Do **not** store database
  credentials or model tokens in the manifest.
- Exact query/search options for each arm, relevant operating-system/runtime
  versions, timestamp, and hashes of saved `raw-runs.json` and
  `normalized-runs.json`.

The archived runs preserve query text, source digest, ranked raw responses and
normalized results, so their **scoring is reproducible**. Not every historical
index binary, model snapshot, and DB setting was frozen in a run manifest, so
bit-for-bit reconstruction of those historical *index builds* is not guaranteed.

For an ablation, hold source, qrels, model, indexer, search settings, and
normalization constant; change one retrieval feature. Compare **paired runs of
the same implementation**, especially Rust on/off versus Rust on/off—not Rust
against the TypeScript lexical-only champion. If the top-ten unit sets are
unchanged, recall and precision must stay the same; only ordering metrics can
move. The issue #6 gate requires at least one metric to improve with no
regression in the other three on the locked fixture, plus per-query/category
review. This is a development gate, not statistical proof of broader quality.
