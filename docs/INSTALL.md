# Installation Guide

## Prerequisites

- macOS (tested on Mac Studio M2 Max, 32GB RAM)
- Python 3.14 (`uv` manages this automatically, or `brew install python@3.14`)
- [uv](https://docs.astral.sh/uv/) — fast Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- [gh](https://cli.github.com/) — GitHub CLI for issues ingestion (`brew install gh && gh auth login`)
- [jq](https://jqlang.github.io/jq/) — JSON processor for MCP hook (`brew install jq`)
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

## 9. Install the nightly learning and ingestion scripts

```bash
ln -sf "$(pwd)/nightly-learn.py" ~/.hindsight/nightly-learn.py
ln -sf "$(pwd)/ingest-issues.py" ~/.hindsight/ingest-issues.py
```

## 10. Schedule with launchd

Install the plist (replacing `__HOME__` with your home directory):

```bash
sed "s|__HOME__|$HOME|g" launchd/io.vectorize.hindsight.nightly.plist \
  > ~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist
```

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

## 16. Restart Cursor

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

## Uninstall

```bash
# Stop and remove all launchd services
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.service.plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.issues.plist
rm ~/Library/LaunchAgents/io.vectorize.hindsight.*.plist

# Remove data and runtime
rm -rf ~/.hindsight ~/.pg0

# Remove Cursor integration
rm ~/.cursor/rules/hindsight-memory.mdc
rm ~/.cursor/hooks.json
rm -rf ~/.cursor/hooks/log-mcp-calls.sh

# Remove MCP entries: delete hindsight, hindsight-docs, hindsight-issues,
# and gopls from ~/.cursor/mcp.json (or restore your previous mcp.json)
```
