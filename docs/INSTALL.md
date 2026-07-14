# Installation Guide

## Prerequisites

- macOS (tested on Mac Studio M2 Max, 32GB RAM)
- Python 3.14 (`uv` manages this automatically, or `brew install python@3.14`)
- [uv](https://docs.astral.sh/uv/) — fast Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- [gh](https://cli.github.com/) — GitHub CLI for issues ingestion (`brew install gh && gh auth login`)
- [jq](https://jqlang.github.io/jq/) — JSON processor for MCP hook (`brew install jq`)
- `pip install cocoindex` (or `uv pip install cocoindex`) — incremental ingestion engine
- For code indexing: `pip install tree-sitter tree-sitter-go` (optional, for custom symbol extraction). The code index supports hybrid search (dense + BM25) out of the box — no additional setup required.
- Google Cloud SDK (`gcloud`) with Application Default Credentials configured
- Vertex AI API enabled on your GCP project
- Claude models enabled on Vertex AI (Haiku 4.5 + Sonnet 4.6)
- Cursor IDE

## 1. Clone the project

```bash
git clone https://github.com/jordigilh/engram.git
cd engram
git config core.hooksPath .githooks
```

## 2. Authenticate with Google Cloud

```bash
gcloud auth application-default login
```

## 3. Create the runtime directory and config

```bash
mkdir -p ~/.hindsight/logs
cp config.env.example ~/.hindsight/config.env
```

Edit `~/.hindsight/config.env` and fill in your GCP project ID:

```bash
$EDITOR ~/.hindsight/config.env
```

> **Important**: `~/.hindsight/config.env` contains your real project IDs and stays
> local. It is never committed to this repo. The pre-commit hook will block any
> attempt to commit actual project IDs.

## 4. Install Hindsight (native)

```bash
uv venv ~/.hindsight/venv --python 3.14
uv pip install --python ~/.hindsight/venv/bin/python \
  'hindsight-api[all]' 'google-cloud-aiplatform>=1.38'
```

This installs Hindsight with embedded PostgreSQL (pg0), local ONNX embeddings,
and local reranker — all running natively on macOS with no container or VM
dependency. Data persists at `~/.pg0/instances/hindsight/data/`.

## 5. Start the service

```bash
./start.sh
```

This sources `~/.hindsight/config.env` and runs the native `hindsight-api` binary.

> **Important**: Use `./start.sh` for development OR launchd for production.
> Do not run both simultaneously — they bind the same port (8888).

For production, install as a launchd service (auto-start on login, auto-restart
on crash):

```bash
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__VERTEXAI_PROJECT__|$(grep VERTEXAI_PROJECT ~/.hindsight/config.env | cut -d= -f2)|g" \
    launchd/io.vectorize.hindsight.service.plist \
    > ~/Library/LaunchAgents/io.vectorize.hindsight.service.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.service.plist
```

> **Note on model names**: Sonnet 4.6 must be specified WITHOUT a version suffix on the
> global endpoint. Haiku 4.5 works with `@20251001`.

## 6. Verify

```bash
curl -s http://localhost:8888/health | python3 -m json.tool
# Expected: {"status": "healthy", "database": "connected"}
```

Test a retain + recall cycle:

```bash
# Retain a fact
curl -s -X POST http://localhost:8888/v1/default/banks/cursor-memory/memories \
  -H "Content-Type: application/json" \
  -d '{"items": [{"content": "Always use table-driven tests in Go with t.Run subtests."}]}'

# Recall it
curl -s -X POST http://localhost:8888/v1/default/banks/cursor-memory/memories/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "Go testing best practices"}' | python3 -m json.tool
```

## 7. Configure Cursor MCP

```bash
cp cursor/mcp.json ~/.cursor/mcp.json
```

> If you already have an `~/.cursor/mcp.json`, merge the `hindsight` entry into
> your existing `mcpServers` object.

## 8. Install Cursor rule

```bash
mkdir -p ~/.cursor/rules
cp cursor/hindsight-memory.mdc ~/.cursor/rules/
```

The included rule is tuned for kubernaut/Go development. For other projects,
customize it — see [Customizing the Rule](#customizing-the-rule) below.

Because this copy step is manual, the deployed `~/.cursor/rules/hindsight-memory.mdc`
and this repo's `cursor/hindsight-memory.mdc` can silently drift apart if one is
edited without the other. After editing either copy, check they're still in sync:

```bash
python3 check-rule-sync.py          # reports drift, exit 1 if any
python3 check-rule-sync.py --fix    # copies canonical -> deployed on drift
```

## 9. Install the nightly learning and ingestion scripts

```bash
ln -sf "$(pwd)/nightly-learn.py" ~/.hindsight/nightly-learn.py
ln -sf "$(pwd)/ingest-issues.py" ~/.hindsight/ingest-issues.py
ln -sf "$(pwd)/correction_gate.py" ~/.hindsight/correction_gate.py
ln -sf "$(pwd)/contradiction_resolution.py" ~/.hindsight/contradiction_resolution.py
ln -sf "$(pwd)/project_scope.py" ~/.hindsight/project_scope.py
```

`correction_gate.py` is the Haiku-based correction-detection gate,
`contradiction_resolution.py` the three-tier contradiction check, and
`project_scope.py` the onboarded-project allowlist gate — all shared by
`nightly-learn.py` and `cocoindex-flows.py` (see [FINDINGS.md](../docs/FINDINGS.md)).
`correction_gate.py` and `contradiction_resolution.py` import from `spike/`, so
also symlink that directory if you haven't already (step 16 does this for
CocoIndex, but `nightly-learn.py` needs it too):

```bash
ln -sf "$(pwd)/spike" ~/.hindsight/spike
```

> **Customize for your projects**: `project_scope.py`'s `ALLOWED_WORKSPACE_PREFIXES`
> hardcodes which Cursor workspaces feed the shared `cursor-memory` retain
> pipeline (currently kubernaut/dcm/engram). Edit that list to match your own
> project(s) before deploying — otherwise `nightly-learn.py` and
> `cocoindex-flows.py` will retain nothing (or the wrong projects' transcripts)
> from your workspaces. See [FINDINGS.md](../docs/FINDINGS.md) 2026-07-13 for
> why this allowlist exists.

## 10. Schedule with launchd

Install the plist (replacing `__HOME__` with your home directory):

```bash
sed "s|__HOME__|$HOME|g" launchd/io.vectorize.hindsight.nightly.plist \
  > ~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist
```

Do the same for the hourly retain-only job:

```bash
sed "s|__HOME__|$HOME|g" launchd/io.vectorize.hindsight.hourly.plist \
  > ~/Library/LaunchAgents/io.vectorize.hindsight.hourly.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.hourly.plist
```

> **Note:** both the hourly and nightly plists run `nightly-learn.py` under
> `~/.hindsight/venv/bin/python3`, not the macOS system Python. This is required
> because `correction_gate.py` (via `spike/classify.py`) calls `litellm`, which
> is only installed in the Hindsight venv — `nightly-learn.py` was pure-stdlib
> before this and ran fine under system Python, but no longer does. If you ever
> revert to `ENGRAM_CORRECTION_DETECTOR=regex` full-time, the venv requirement
> goes away, but there's no harm in leaving the interpreter pointed at the venv
> either way.

## 11. Ingest project documentation (Knowledge RAG)

This creates a `kubernaut-docs` knowledge bank and ingests the published documentation
for embedding-based recall (zero LLM cost):

```bash
python3 ingest-docs.py --docs-dir ~/go/src/github.com/jordigilh/kubernaut-docs/docs
```

The script creates the bank, configures `chunks` extraction mode, and ingests all
markdown files. This only needs to be run once (or re-run when docs are updated).

## 12. Ingest GitHub issues (Knowledge RAG)

This creates a `kubernaut-issues` knowledge bank and ingests open issues plus
recently closed issues from the kubernaut repository:

```bash
python3 ingest-issues.py
```

Options:
- `--open-only` — skip closed issues
- `--days 180` — include closed issues from last 180 days (default: 90)
- `--repo org/other-repo` — target a different repository

Re-run periodically to pick up new issues. The script uses `document_id` per
issue, so re-ingestion is idempotent. To schedule nightly (daily at 1:00 AM):

```bash
sed "s|__HOME__|$HOME|g" launchd/io.vectorize.hindsight.issues.plist \
  > ~/Library/LaunchAgents/io.vectorize.hindsight.issues.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.issues.plist
```

## 13. Create mental models (Knowledge Graph)

Mental models are LLM-synthesized documents that sit above raw facts in the recall
hierarchy. They provide pre-digested, coherent context blocks — reducing the need
for the agent to synthesize scattered individual facts at query time.

```bash
python3 create-mental-models.py
```

This creates 9 mental models across all three banks and triggers initial refresh
(~$0.50 total, one-time Sonnet 4.6 cost). To check status:

```bash
python3 create-mental-models.py --list
```

Behavioral models (in `cursor-memory`) auto-refresh after nightly consolidation.
Issues-bank models refresh nightly (after issue ingestion at 1:00 AM). Docs-bank
models refresh manually when documentation is updated:

```bash
python3 create-mental-models.py --refresh
```

## 14. Install gopls MCP for Go code intelligence

```bash
go install golang.org/x/tools/gopls@latest
```

The `gopls` entry is already in `cursor/mcp.json`. It provides type-aware Go
intelligence (implementations, references, definitions) directly in Cursor without
ingesting source code.

## 15. Install the observability hook

This hook logs every MCP call (hindsight, hindsight-docs, hindsight-issues, gopls)
for effectiveness monitoring:

```bash
mkdir -p ~/.cursor/hooks
cp cursor/hooks/log-mcp-calls.sh ~/.cursor/hooks/
chmod +x ~/.cursor/hooks/log-mcp-calls.sh
sed "s|__HOME__|$HOME|g" cursor/hooks.json > ~/.cursor/hooks.json
```

## 16. CocoIndex Setup

CocoIndex replaces the batch ingestion scripts (`ingest-docs.py`, `ingest-issues.py`)
with continuous, incremental sync for docs, issues, code, and transcripts.

### Install CocoIndex into the Hindsight venv

```bash
uv pip install --python ~/.hindsight/venv/bin/python cocoindex
```

### Symlink flow and search scripts

```bash
ln -sf "$(pwd)/cocoindex-flows.py" ~/.hindsight/cocoindex-flows.py
ln -sf "$(pwd)/cocoindex-search.py" ~/.hindsight/cocoindex-search.py
```

`cocoindex-flows.py` also imports `correction_gate.py`, `contradiction_resolution.py`,
and `project_scope.py` directly — make sure step 9's symlinks are in place before
running this, or CocoIndex's transcript app will fail to start with a
`ModuleNotFoundError`.

### Configure source directories

Add the following to `~/.hindsight/config.env`:

```bash
ENGRAM_DOCS_DIR=~/go/src/github.com/jordigilh/kubernaut-docs/docs
ENGRAM_CODE_DIR=~/go/src/github.com/jordigilh/kubernaut
# Optional: issues poll interval in seconds (default: 300 = 5 min)
# ENGRAM_ISSUES_POLL_SECONDS=300
```

### Run initial backfill

```bash
python3 cocoindex-flows.py --mode backfill
```

This processes all existing docs, issues, code, and transcripts. Subsequent runs
use delta processing (only changed content is re-ingested).

### Install launchd plist (continuous sync)

```bash
sed "s|__HOME__|$HOME|g" launchd/io.vectorize.cocoindex.service.plist \
  > ~/Library/LaunchAgents/io.vectorize.cocoindex.service.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.cocoindex.service.plist
```

### Verify

```bash
# Check all four flows started
grep "Starting\|Fetched\|poll:" ~/.hindsight/logs/cocoindex-stderr.log | tail -10

# Check issues + PRs are fully indexed
grep "Fetched.*from" ~/.hindsight/logs/cocoindex-stderr.log | tail -5
```

You should see all four apps starting (docs, code, transcripts, issues) and
issue poll cycles completing with the full count of issues + PRs. See
[CocoIndex Operations](COCOINDEX.md) for monitoring and troubleshooting details.

## 17. Migration from Old Scripts

After verifying CocoIndex is syncing successfully, the old batch ingestion
scripts are no longer needed:

```bash
# Unload the old issues ingestion plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.issues.plist
rm ~/Library/LaunchAgents/io.vectorize.hindsight.issues.plist
```

The old scripts (`ingest-docs.py` and `ingest-issues.py`) remain in the repo
for reference but are superseded by CocoIndex flows. You can verify data parity
by comparing recall results before and after migration — CocoIndex ingests the
same content through the Hindsight retain API, so recall quality should be
identical or better (due to continuous freshness).

## 18. Restart Cursor

Reload the Cursor window (or restart the app) so it picks up the new MCP config,
rule, and hook.

## Verification

After restarting Cursor, open a new chat. The agent should now call `recall_memory`
before responding.

To manually test the nightly pipeline:

```bash
python3 ~/.hindsight/nightly-learn.py
```

Check results:

```bash
cat ~/.hindsight/logs/$(date +%Y-%m-%d).json | python3 -m json.tool
```

Generate an effectiveness report:

```bash
python3 report.py          # last 7 days
python3 report.py --days 30  # last 30 days
python3 report.py --json     # machine-readable
```

See [METRICS.md](METRICS.md) for full details on what's tracked and how to
interpret the results.

---

## Running the Test Suite

The `tests/` directory has a `pytest` regression suite covering the shared
modules (`correction_gate.py`, `contradiction_resolution.py`, `project_scope.py`),
the `spike/hindsight_client.py` recall client, `review-contradictions.py`'s
approve/reject/skip/quit flow, and the core retain logic in `nightly-learn.py`
and `cocoindex-flows.py`. Added 2026-07-13 after three real bugs shipped to
production in one session with zero automated coverage catching any of them
(see [FINDINGS.md](FINDINGS.md)).

Install the dev dependency into the same venv the production scripts run
under:

```bash
uv pip install --python ~/.hindsight/venv/bin/python -r requirements-dev.txt
```

Run the suite:

```bash
~/.hindsight/venv/bin/python3 -m pytest tests/ -v
```

The suite is fully offline — every LLM call (Haiku classification, Sonnet
contradiction check), Hindsight API call, and CocoIndex file-watch is mocked
via `pytest`'s `monkeypatch` fixture, so it runs in well under a second and
never touches your live `~/.hindsight/` data or costs any tokens. `conftest.py`
adds the repo root and `spike/` to `sys.path` and provides fixtures
(`nightly_learn`, `cocoindex_flows`, `review_contradictions`, `purge_script`)
for loading the hyphenated production scripts as importable modules.

---

## Troubleshooting

### Service won't start
```bash
launchctl list | grep hindsight
tail -50 ~/.hindsight/logs/hindsight-stderr.log
```

### Recall returns empty results
The memory bank needs at least one retained item. Run the nightly script manually or retain a test memory.

### Retain fails with "Could not resolve project_id"
Ensure `VERTEXAI_PROJECT` and `GOOGLE_CLOUD_PROJECT` are set in `~/.hindsight/config.env`.

### Reflect returns 404
Sonnet 4.6 on the global endpoint requires the model name WITHOUT a version suffix. Use `vertex_ai/claude-sonnet-4-6`, not `vertex_ai/claude-sonnet-4-6@20250929`.

### ADC token expired
```bash
gcloud auth application-default login
launchctl kickstart -k gui/$(id -u)/io.vectorize.hindsight.service
```

### Manually restart the service
```bash
launchctl kickstart -k gui/$(id -u)/io.vectorize.hindsight.service
```

---

## Upgrading

```bash
uv pip install --python ~/.hindsight/venv/bin/python -U 'hindsight-api[all]'
launchctl kickstart -k gui/$(id -u)/io.vectorize.hindsight.service
```

Verify after upgrade:

```bash
curl -s http://localhost:8888/health | python3 -m json.tool
```

---

## Customizing the Rule

The included `hindsight-memory.mdc` rule is tailored for kubernaut (a Go operator
project with CocoIndex code search and gopls). Adapt it for your own project
by copying one of the ready-made examples below and tweaking the domain triggers.

### Ready-made examples

Example rules live in `cursor/examples/`. Each is a complete, copy-ready `.mdc`
file with the planning gate, mid-session re-recall, and phase-based triggers
already wired in:

| Example | Stack | File |
|---------|-------|------|
| Go operator | Go, K8s, CRDs, gopls, CocoIndex | [`cursor/examples/go-operator.mdc`](../cursor/examples/go-operator.mdc) |
| Python web app | Python, Django/Flask/FastAPI, CocoIndex | [`cursor/examples/python-web.mdc`](../cursor/examples/python-web.mdc) |
| Rust systems | Rust, unsafe, traits, crates, CocoIndex | [`cursor/examples/rust-systems.mdc`](../cursor/examples/rust-systems.mdc) |
| TypeScript/React | TS, React, components, hooks, CocoIndex | [`cursor/examples/typescript-react.mdc`](../cursor/examples/typescript-react.mdc) |
| Minimal | Any stack (language-agnostic), CocoIndex | [`cursor/examples/minimal.mdc`](../cursor/examples/minimal.mdc) |

**To install an example:**

```bash
# Copy to your global Cursor rules (applies to all projects)
cp cursor/examples/python-web.mdc ~/.cursor/rules/hindsight-memory.mdc

# Or copy to a specific project (applies only to that repo)
mkdir -p /path/to/your/project/.cursor/rules
cp cursor/examples/python-web.mdc /path/to/your/project/.cursor/rules/hindsight-memory.mdc
```

### What each example includes

Every example rule has these sections, which reflect empirical findings from
the kubernaut project:

1. **When to recall (MUST)** — domain-specific triggers for first-turn recall
2. **Before planning or implementing (MANDATORY GATE)** — forces a recall of
   project methodology before any plan is proposed. This prevents the agent from
   defaulting to generic patterns instead of your established conventions
3. **Mid-session re-recall** — triggered after ~20 agent turns or after context
   summarization. Empirical data showed 61% of corrections occur in the second
   half of sessions, largely due to summarization silently dropping recalled
   conventions
4. **Code search via CocoIndex** — directs the agent to use `cocoindex_search`
   for semantic code exploration (finding code by concept/meaning) instead of
   relying on Grep/SemanticSearch. Requires CocoIndex setup (step 16 above)
5. **Phase-based triggers** — recall the right bank or call `cocoindex_search`
   when transitioning to a new phase (planning, testing, API design, debugging,
   code exploration, refactoring)
6. **Skip criteria** — prevents recall spam on trivial follow-ups
7. **Do NOT retain** — blocks in-session retain calls (extraction runs nightly)

### Adapting an example

When customizing, change:

1. **Domain triggers** — replace language/framework mentions with your stack
2. **Banks** — adjust which banks exist (`hindsight` is always present; add
   `hindsight-docs` and `hindsight-issues` if you ingest docs/issues)
3. **Phase-based queries** — tailor query focus to your project's terminology
   (e.g., "pytest fixtures" vs "table-driven tests")
4. **Language tooling** — add your language's MCP if available (gopls for Go,
   rust-analyzer for Rust, etc.)

### Key principles

- **Be specific about triggers** — generic rules get ignored; domain-specific
  triggers (language, framework, problem type) get followed
- **Include the planning gate** — without it, the agent will propose plans
  based on generic knowledge rather than your project's methodology
- **Include mid-session re-recall** — long sessions lose context to
  summarization; re-recalling counteracts this
- **Include skip criteria** — prevents recall spam on trivial interactions
- **One rule file** — don't split across multiple `.mdc` files; `alwaysApply: true`
  means it's always loaded

---

## Uninstall

```bash
# Stop and remove all launchd services
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.service.plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.issues.plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.cocoindex.service.plist
rm ~/Library/LaunchAgents/io.vectorize.hindsight.*.plist
rm ~/Library/LaunchAgents/io.vectorize.cocoindex.*.plist

# Remove data and runtime
rm -rf ~/.hindsight ~/.pg0

# Remove Cursor integration
rm ~/.cursor/rules/hindsight-memory.mdc
rm ~/.cursor/hooks.json
rm -rf ~/.cursor/hooks/log-mcp-calls.sh

# Remove MCP entries: delete hindsight, hindsight-docs, hindsight-issues,
# and gopls from ~/.cursor/mcp.json (or restore your previous mcp.json)
```
