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

Create or edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "hindsight": {
      "type": "http",
      "url": "http://localhost:8888/mcp/cursor-memory/"
    }
  }
}
```

## 8. Create Cursor rule

Create `~/.cursor/rules/hindsight-memory.mdc`:

```markdown
---
description: Recall relevant patterns from Hindsight memory before responding
alwaysApply: true
---

Before generating each response, use the Hindsight MCP `recall_memory` tool to
retrieve relevant patterns and learned context. Include recalled patterns in your
reasoning when they apply.

Do NOT call retain_memory during sessions — memory extraction happens in a
nightly batch process.

When recalling, search for:
- Patterns related to the current task or technology
- User preferences and coding conventions
- Past mistakes and their corrections for similar work
- Architecture decisions relevant to this codebase
```

## 9. Schedule the nightly learning script

The script `nightly-learn.py` scans Cursor transcripts for corrections and
feeds them to Hindsight. Install it:

```bash
ln -sf "$(pwd)/nightly-learn.py" ~/.hindsight/nightly-learn.py
chmod +x nightly-learn.py
```

## 10. Schedule with launchd

Create `~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.vectorize.hindsight.nightly</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOUR_USERNAME/.hindsight/nightly-learn.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>0</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/YOUR_USERNAME/.hindsight/logs/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USERNAME/.hindsight/logs/launchd-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

> Replace `YOUR_USERNAME` with your macOS username.

Load it:

```bash
launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist
```

## 11. Restart Cursor

Reload the Cursor window (or restart the app) so it picks up the new MCP config and rule.

## Verification

After restarting Cursor, open a new chat. The agent should now call `recall_memory` before responding. You can verify in the Hindsight control plane at http://localhost:9999.

To manually test the nightly pipeline:

```bash
python3 ~/.hindsight/nightly-learn.py
```

Check results:

```bash
cat ~/.hindsight/logs/$(date +%Y-%m-%d).json | python3 -m json.tool
```

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
