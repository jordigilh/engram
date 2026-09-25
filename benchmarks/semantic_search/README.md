# Semantic search query suites

Use [`QUALITY_EVALUATION.md`](./QUALITY_EVALUATION.md) for the relevance
judging protocol, ingestion-versus-retrieval failure taxonomy, and metrics.
The source-grounded draft targets for the historical feature-branch suite are in
[`kubernaut_fix-2442_8f3bc5a2_2026-09-23.relevance-draft.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.relevance-draft.json);
those targets are not gold labels. The synthetic fixture below is the primary
regression lane for zvec-grep retrieval experiments; the Kubernaut artifacts
later on this page are exploratory real-repository context, not a quality gate.
The four issue-#6 proposal qevals and current per-language enablement evidence
are consolidated in [`PROPOSAL_QEVAL_REPORT.md`](./PROPOSAL_QEVAL_REPORT.md).

## Golden synthetic fixtures

The end-to-end fixture construction, index/replay commands, normalization,
metric formulas, and per-run provenance requirements are in
[`SYNTHETIC_EVALUATION.md`](./SYNTHETIC_EVALUATION.md).
The independent Go, Python, Rust, and TypeScript qeval lanes and measured
baseline/candidate comparison are in
[`MULTILANGUAGE_PAIRED_RESULTS.md`](./MULTILANGUAGE_PAIRED_RESULTS.md), with
the reproducible fixture/paired-run protocol in
[`MULTILANGUAGE_PROTOCOL.md`](./MULTILANGUAGE_PROTOCOL.md).

Use the small, source-authored fixture suite for repeatable regression checks:

- [`fixtures/go-workflow-discovery-v1/`](./fixtures/go-workflow-discovery-v1/): 38 qualified Go source units, realistic distractors, and eight query intents.
- [`fixtures/go-workflow-discovery-v1/truth.json`](./fixtures/go-workflow-discovery-v1/truth.json): source-authored relevant targets; it is independent of every search backend.
- [`fixtures/go-workflow-discovery-v1/qrels.json`](./fixtures/go-workflow-discovery-v1/qrels.json): complete adjudicated qrels covering the full 38-unit fixture universe.
- [`fixtures/go-workflow-discovery-v1/QEVAL_RESULTS.md`](./fixtures/go-workflow-discovery-v1/QEVAL_RESULTS.md): measured synthetic qevals, run artifacts, and the zvec-grep issue tracking decisions.

Verify the fixture source digest and source-authored qrels with
`python3 -m pytest -q tests/test_synthetic_semantic_search.py`. Capture ranked
results against the same fixture and normalize to its qualified symbol IDs
before scoring. The [protocol](./SYNTHETIC_EVALUATION.md) gives the full
index, replay, and evaluation commands.

The replay adapter is [`scripts/replay_synthetic_semantic_search.py`](../../scripts/replay_synthetic_semantic_search.py).
It records raw responses and maps each result to every manifest symbol whose
source span intersects the returned chunk or line range. `backend_rank` keeps
the original backend rank; consequently, the evaluator's `@10` cutoff is over
normalized source units, while the raw replay remains limited to ten backend
results. The manifest covers all 38 source declarations so backend distractors
remain explicit grade-0 candidates rather than becoming unjudged failures.

The completed 2026-09-23 replay is under
`fixtures/go-workflow-discovery-v1/replays/2026-09-23/`.

`kubernaut_workflow_discovery.json` is the versioned prompt set used for the
Kubernaut CocoIndex vs. zvec-grep comparison. It preserves the exact query text
and the one test-only probe that was excluded from production-code scoring.
The suite intentionally has no expected-file list: the earlier file labels were
incomplete and are not gold relevance judgments.

## Replaying the prompts

Run the same active queries against each backend, preserving the query strings
and result limit. First build or select indexes over the same source snapshot
and file scope. For the historical profile, the zvec workspace root must
contain only the 1,071-file production-Go scope described below. Index it with
the same model and file-size/visibility options before querying:

```sh
zg index "$ZVEC_ROOT" --name kubernaut-workflow-discovery \
  --embedding local/potion-code-16m-v2 --model-cache "$MODEL_CACHE" \
  --device cpu --max-filesize 2M --hidden --no-ignore --mode direct
```

Then run from that indexed workspace root:

```sh
python - <<'PY'
import json
import subprocess
from pathlib import Path

suite = json.loads(Path("benchmarks/semantic_search/kubernaut_workflow_discovery.json").read_text())
for case in suite["queries"]:
    if case["status"] != "active":
        continue
    print(f"\n## {case['id']}: {case['query']}")
    subprocess.run([
        "zg", "query", "--mode", "direct", "--refresh", "off",
        "--limit", "10", "--human", "--preview", "short", "--no-color",
        "--hybrid", case["query"],
    ], check=True)
PY
```

For an isolated CocoIndex comparison, materialize the matched source root into
a disposable table (the root must already contain the same filtered corpus):

```sh
COCOINDEX_DB="$COCOINDEX_STATE_DB" COCOINDEX_PG_URL="$COCOINDEX_PG_URL" \
  PYTHONPATH=src python -m engram.code_overlay_cocoindex \
  --root "$SOURCE_SCOPE_ROOT" --repo-tag kubernaut --table "$COCOINDEX_TABLE"
```

Replay the same suite through `engram.search.kubernaut.search_code()`:

```sh
PYTHONPATH=src python - <<'PY'
import json
import os
from pathlib import Path

from engram.search.kubernaut import search_code

suite = json.loads(Path("benchmarks/semantic_search/kubernaut_workflow_discovery.json").read_text())
table = os.environ.get("COCOINDEX_TABLE")
for case in suite["queries"]:
    if case["status"] != "active":
        continue
    options = {"table": table} if table else {}
    results = search_code(
        case["query"], limit=10, mode="hybrid",
        repo="kubernaut", branch="main", **options,
    )
    print(json.dumps({"id": case["id"], "query": case["query"], "results": results}, default=str))
PY
```

When using an isolated CocoIndex snapshot, build its table from the same
pre-filtered source root and set `COCOINDEX_TABLE` to that table name. Record
the full ranked chunks from both systems, not just unique file paths. Keep a
per-run copy of the manifest path, branch/commit, dirty-worktree digest, indexed
scope, model/configuration, and result limit with the output.

## Historical run captured by this suite

The original shadow run used Kubernaut branch
`fix/2442-workflow-discovery-membership`, HEAD
`d7df5737ae9d14413ef68466324639f525574fee`, base commit
`70b9d854cc21b83e8740909d69b2f6aa63d3d6c4`, and dirty-worktree digest
`359a9c40c6e25da074cc3eafeace24d7a6e00af88b9d7153dae80bb49d04c915`. It
indexed 1,071 production Go files (13,581,851 bytes), excluding tests, vendor,
and `zz_generated*`; zvec used `local/potion-code-16m-v2`, a 2 MB maximum file
size, and hidden/no-ignore scanning. CocoIndex used hybrid search with
`sentence-transformers/all-MiniLM-L6-v2` and 1,000-character code chunks with
300-character overlap in isolated table
`code_embeddings_shadow_kubernaut_d7df5737_canon`. That table was dropped after
the run.

Because this snapshot included uncommitted Kubernaut changes, its commit alone
does not reproduce the corpus; the digest identifies the state but is not a
copy of the dirty files. The exact query suite and run metadata are recorded
here so future runs can use the same prompts and record a reproducible source
state. The historical comparison and triage are tracked in
[Engram issue #113](https://github.com/jordigilh/engram/issues/113).

## Branch-pinned follow-up

`search_code(branch="main")` excludes release-tagged rows; it does not pin the
indexed source to a Git commit. The deployed Kubernaut CocoIndex service watches
the configured live checkout, so record and verify that checkout before treating
its default `kubernaut` rows as `main`.

The 2026-09-23 main-to-main run is recorded in
[`kubernaut_main_70b9d854_2026-09-23.json`](./kubernaut_main_70b9d854_2026-09-23.json).
It uses a clean `origin/main` snapshot at `70b9d854cc21b83e8740909d69b2f6aa63d3d6c4`,
the same filtered 1,067-file production-Go scope for both engines, a disposable
CocoIndex shadow table, and a separate zvec workspace index. The live
`cocoindex.code_embeddings` table was not changed. Generated follow-up prompts
are kept separately in
[`kubernaut_workflow_discovery_followups_v1.json`](./kubernaut_workflow_discovery_followups_v1.json)
so they remain distinguishable from the original prompt set.

The historical branch-specific run used `fix/2442-workflow-discovery-membership`
at `8f3bc5a2d7da5262553a0919f9faecb83d61a09a`; its
comparison is recorded in
[`kubernaut_fix-2442_8f3bc5a2_2026-09-23.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.json).
The production-Go path inventory matched the live CocoIndex table at 1,072
distinct files. The main snapshot is a separate run, not part of this comparison.

The pinned three-backend replay status is recorded in
[`kubernaut_fix-2442_8f3bc5a2_2026-09-23.reproduction-blocker.md`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.reproduction-blocker.md).
Full ranked output for Sense v1.15.1, the disposable CocoIndex table, and the
rebuilt zvec index is preserved separately. The pooled candidates have now been
normalized and adjudicated.

The normalized, backend-separated candidate artifacts are:

- [`kubernaut_fix-2442_8f3bc5a2_2026-09-23.normalized-runs.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.normalized-runs.json): evaluator-shaped runs with stable source-span IDs.
- [`kubernaut_fix-2442_8f3bc5a2_2026-09-23.backend-mapping.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.backend-mapping.json): rank and backend provenance for each normalized result.
- [`kubernaut_fix-2442_8f3bc5a2_2026-09-23.blinded-pool.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.blinded-pool.json): 300 source-grounded candidates used for adjudication; it contains no backend labels or draft grades.
- [`kubernaut_fix-2442_8f3bc5a2_2026-09-23.qrels-first-pass.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.qrels-first-pass.json): agent-assigned grades for all 300 candidates, explicitly pending human review.
- [`kubernaut_fix-2442_8f3bc5a2_2026-09-23.human-review-decisions.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.human-review-decisions.json): complete interactive review log covering all 300 candidates.
- [`kubernaut_fix-2442_8f3bc5a2_2026-09-23.qrels-adjudicated.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.qrels-adjudicated.json): final qrels with `judgment_status=adjudicated`.
- [`kubernaut_fix-2442_8f3bc5a2_2026-09-23.metrics-k10.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.metrics-k10.json): historical nDCG, MRR, recall, and precision at 10; do not use these as the zvec-grep qeval quality gate.

Rebuild those artifacts with `scripts/normalize_semantic_search.py` after
replaying the raw runs. The script intentionally emits
`judgment_status=pending-adjudication` and never assigns relevance grades.
The first-pass qrels are also evaluator-ineligible by design; review the
ambiguous query surfaces and change both `judgment_status` and
`candidate_pool_complete` only after human adjudication.

The adjudicated run was produced with `scripts/finalize_semantic_qrels.py` and
scored with `scripts/evaluate_semantic_search.py --k 10`.
