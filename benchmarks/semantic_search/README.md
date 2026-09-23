# Semantic search query suites

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

The current iteration is focused on the live `fix/2442-workflow-discovery-membership`
checkout at `8f3bc5a2d7da5262553a0919f9faecb83d61a09a`; its branch-specific
comparison is recorded in
[`kubernaut_fix-2442_8f3bc5a2_2026-09-23.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.json).
The source inventory matched the live CocoIndex Kubernaut path set at 1,072 Go
files. The main snapshot above is retained as a separate historical run and is
not part of this branch-focused iteration.

The current iteration is focused on the live `fix/2442-workflow-discovery-membership`
checkout at `8f3bc5a2d7da5262553a0919f9faecb83d61a09a`; its branch-specific
comparison is recorded in
[`kubernaut_fix-2442_8f3bc5a2_2026-09-23.json`](./kubernaut_fix-2442_8f3bc5a2_2026-09-23.json).
The production-Go path inventory matched the live CocoIndex table at 1,072
distinct files. The main snapshot is retained as a separate historical run and
is not used for this branch-focused iteration.
