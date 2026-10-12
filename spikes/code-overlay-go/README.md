# Go Overlay Planner Spike

This is a control-plane spike, not a replacement indexer. It discovers the
current Git/worktree delta and optionally stages eligible files for a backend.

```sh
go build -o /tmp/code-overlay-go .
/tmp/code-overlay-go \
  -worktree /path/to/kubernaut \
  -base origin/main \
  -repository kubernaut \
  -stage-dir /tmp/kubernaut-overlay-staged
```

The manifest includes branch, HEAD, merge-base, working-tree digest, changed
files, tombstones, and separate Codanna/CocoIndex eligible-file counts.

The current measurement shows Go and Python planner overhead are both about one
second for a heavily dirty Kubernaut worktree. That cost is dominated by Git
and hashing. A long-lived Go manager should cache the manifest and refresh only
after filesystem events. Rust is not justified unless profiling shows this
cached control plane is still a meaningful part of query latency.

Codanna materialization must not run synchronously in the query path. The
current spike exposes equivalent Python controls for comparison:

```sh
python scripts/code_overlay.py build --backend codanna \
  --codanna-production-only --codanna-no-semantic \
  --worktree /path/to/kubernaut --base origin/main
```

The measured large-file outlier was three generated OpenAPI Go files: semantic
indexing took about 230 seconds and structural indexing about 103 seconds. Four
parallel semantic shards reduced wall time to about 22 seconds, but lose
cross-file Codanna relationships. Prefer a persistent background watcher and
serve the last verified overlay while a replacement builds.
