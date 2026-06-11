# Installation Guide

## Prerequisites

- macOS (tested on Mac Studio M2 Max, 32GB RAM)
- [Podman](https://podman.io/) or Docker
- Google Cloud SDK (`gcloud`) with Application Default Credentials configured
- Vertex AI API enabled on your GCP project
- Claude models enabled on Vertex AI (Haiku 4.5 + Sonnet 4.6)
- Cursor IDE

## 1. Clone the project

```bash
git clone https://github.com/jordigilh/recollect.git
cd recollect
git config core.hooksPath .githooks
```

## 2. Authenticate with Google Cloud

```bash
gcloud auth application-default login
```

## 3. Create the runtime directory and config

```bash
mkdir -p ~/.hindsight/data ~/.hindsight/logs
cp config.env.example ~/.hindsight/config.env
```

Edit `~/.hindsight/config.env` and fill in your GCP project ID:

```bash
$EDITOR ~/.hindsight/config.env
```

> **Important**: `~/.hindsight/config.env` contains your real project IDs and stays
> local. It is never committed to this repo. The pre-commit hook will block any
> attempt to commit actual project IDs.

## 4. Build the custom image

```bash
podman build -t hindsight-vertexai .
```

> The official Hindsight image doesn't include `google-cloud-aiplatform` needed
> for Vertex AI. The Dockerfile in this repo adds it.

## 5. Start the container

```bash
./start.sh
```

This reads `~/.hindsight/config.env` and passes it to podman via `--env-file`.
No secrets ever appear in command history or process listings.

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

## 9. Install the nightly learning script

```bash
ln -sf "$(pwd)/nightly-learn.py" ~/.hindsight/nightly-learn.py
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
issue, so re-ingestion is idempotent. To schedule weekly (Mondays at 1 AM):

```bash
sed "s|__HOME__|$HOME|g" launchd/io.vectorize.hindsight.issues.plist \
  > ~/Library/LaunchAgents/io.vectorize.hindsight.issues.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.issues.plist
```

## 13. Install gopls MCP for Go code intelligence

```bash
go install golang.org/x/tools/gopls@latest
```

The `gopls` entry is already in `cursor/mcp.json`. It provides type-aware Go
intelligence (implementations, references, definitions) directly in Cursor without
ingesting source code.

## 14. Install the observability hook

This hook logs every MCP call (hindsight, hindsight-docs, gopls) for effectiveness
monitoring:

```bash
cp cursor/hooks.json ~/.cursor/hooks.json
mkdir -p ~/.cursor/hooks
cp cursor/hooks/log-mcp-calls.sh ~/.cursor/hooks/
chmod +x ~/.cursor/hooks/log-mcp-calls.sh
```

## 15. Restart Cursor

Reload the Cursor window (or restart the app) so it picks up the new MCP config,
rule, and hook.

## Verification

After restarting Cursor, open a new chat. The agent should now call `recall_memory`
before responding. You can verify in the Hindsight control plane at http://localhost:9999.

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

### Container won't start
```bash
podman logs hindsight
```

### Recall returns empty results
The memory bank needs at least one retained item. Run the nightly script manually or retain a test memory.

### Retain fails with "No module named vertexai"
The custom image wasn't built correctly. Rebuild:
```bash
podman build --no-cache -t hindsight-vertexai ~/.hindsight/
podman rm -f hindsight && # re-run the podman run command
```

### Retain fails with "Could not resolve project_id"
Ensure `VERTEXAI_PROJECT` and `GOOGLE_CLOUD_PROJECT` are set in the container environment.

### Reflect returns 404
Sonnet 4.6 on the global endpoint requires the model name WITHOUT a version suffix. Use `vertex_ai/claude-sonnet-4-6`, not `vertex_ai/claude-sonnet-4-6@20250929`.

### ADC token expired
```bash
gcloud auth application-default login
podman restart hindsight
```

---

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist
rm ~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist
podman rm -f hindsight
podman rmi hindsight-vertexai
rm -rf ~/.hindsight
rm ~/.cursor/rules/hindsight-memory.mdc
# Remove "hindsight" entry from ~/.cursor/mcp.json
```
