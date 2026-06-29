# New Project Setup Guide

How to add a new GitHub organization/project to the Engram knowledge system with full isolation from existing projects.

## Architecture

Each project gets:
- **Dedicated Hindsight banks**: `<project>-docs` and `<project>-issues` for isolated memory
- **Shared `cursor-memory` bank**: Behavioral corrections and coding conventions are universal
- **Dedicated pgvector table**: `cocoindex.<project>_code_embeddings` for code search isolation
- **Dedicated CocoIndex flows**: Separate ingestion script with its own state database
- **Dedicated launchd service**: Independent process lifecycle
- **Workspace-level MCP config**: `.cursor/mcp.json` in each repo so Cursor only sees relevant servers

## Prerequisites

- Engram repository cloned at `~/go/src/github.com/jordigilh/engram`
- Hindsight API running on `localhost:8888`
- PostgreSQL with pgvector running on `localhost:5432`
- CocoIndex Python environment at `~/.hindsight/venv/`
- `gh` CLI authenticated with access to the target organization

## Steps

### 1. Fork and Clone Repositories

Fork all active (non-archived) repositories from the organization:

```bash
# List active repos
gh api orgs/<org>/repos --jq '.[] | select(.archived == false) | .name' --paginate

# Fork each
for repo in <list>; do
  gh repo fork "<org>/$repo" --clone=false
done

# Clone to local directory
mkdir -p ~/go/src/github.com/<org>
cd ~/go/src/github.com/<org>
for repo in <list>; do
  gh repo clone "jordigilh/$repo" "$repo" -- --origin fork
done
```

### 2. Create Hindsight Banks

```bash
curl -X PUT http://localhost:8888/v1/default/banks/<project>-docs \
  -H 'Content-Type: application/json' \
  -d '{"description": "<Project> architecture docs, enhancements, guides"}'

curl -X PUT http://localhost:8888/v1/default/banks/<project>-issues \
  -H 'Content-Type: application/json' \
  -d '{"description": "GitHub issues and PRs from all active <project> repositories"}'
```

### 3. Create Mental Models

Use the Hindsight API or MCP to create mental models for each bank:

**`<project>-docs` bank:**
- `<project>-architecture`: Trigger on architecture, components, data flow questions
- `<project>-enhancements`: Trigger on enhancement proposals, design decisions
- `<project>-api-contracts`: Trigger on API contracts, service types

**`<project>-issues` bank:**
- `active-priorities`: Trigger on open issues, priorities, project direction
- `known-bugs`: Trigger on known bugs, root causes, workarounds

### 4. Create CocoIndex Flows

Create `<project>-cocoindex-flows.py` adapted from `cocoindex-flows.py`:

- Three apps: docs, issues, code
- Banks: `<project>-docs`, `<project>-issues`
- pgvector table: `cocoindex.<project>_code_embeddings` (isolated from other projects)
- Separate CocoIndex state DB: `~/.hindsight/<project>-cocoindex.db`
- Environment variables prefixed with `<PROJECT>_*`

### 5. Create Code Search Server

Create `<project>-cocoindex-search.py` adapted from `cocoindex-search.py`:

- Queries `cocoindex.<project>_code_embeddings` table
- MCP server name: `<project>-code`
- Tool name: `<project>_code_search`
- Same hybrid search (dense + BM25 + RRF fusion)

### 6. Create launchd Service

Create `launchd/io.vectorize.cocoindex.<project>.plist`:

- Runs `<project>-cocoindex-flows.py` in live mode
- Environment variables for all repo paths
- Separate log files: `~/.hindsight/logs/<project>-cocoindex-{stdout,stderr}.log`
- KeepAlive: true

Install and start:

```bash
# Replace __HOME__ with actual home directory
sed "s|__HOME__|$HOME|g" launchd/io.vectorize.cocoindex.<project>.plist \
  > ~/Library/LaunchAgents/io.vectorize.cocoindex.<project>.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.cocoindex.<project>.plist
```

### 7. Configure Workspace-Level MCP

Create `.cursor/mcp.json` in each project repository:

```json
{
  "mcpServers": {
    "hindsight-docs": {
      "type": "http",
      "url": "http://localhost:8888/mcp/<project>-docs/"
    },
    "hindsight-issues": {
      "type": "http",
      "url": "http://localhost:8888/mcp/<project>-issues/"
    },
    "cocoindex-code": {
      "command": "/Users/jgil/.hindsight/venv/bin/python3",
      "args": ["/Users/jgil/.hindsight/<project>-cocoindex-search.py"],
      "type": "stdio",
      "env": {
        "COCOINDEX_PG_URL": "postgresql://hindsight:hindsight@localhost:5432/hindsight"
      }
    }
  }
}
```

The workspace-level config uses the same server **names** as kubernaut (`hindsight-docs`, `hindsight-issues`, `cocoindex-code`) but points to different backends. Cursor rules reference these server names, so the same `recall` calls work across projects.

### 8. Slim the Global MCP Config

The global `~/.cursor/mcp.json` should only contain shared servers:

```json
{
  "mcpServers": {
    "hindsight": {
      "type": "http",
      "url": "http://localhost:8888/mcp/cursor-memory/"
    },
    "gopls": {
      "command": "gopls",
      "args": ["mcp"],
      "type": "stdio"
    }
  }
}
```

Project-specific servers are defined at workspace level and override global ones when names collide.

### 9. Create Cursor Rule

Generate `.cursor/rules/hindsight-memory.mdc` from the template:

#### a. Create a project vars file

Create `cursor/projects/<project>.vars`:

```bash
DOMAIN_TRIGGERS="Go code, <project>, or any <domain-specific> work"
DOCS_BANK="<project>-docs"
DOCS_BANK_DESCRIPTION="<project> architecture, API/CRD contracts, operations"
ISSUES_BANK="<project>-issues"
CODE_SEARCH_TOOL="<project>_code_search"
CODE_SEARCH_SERVER="<project>-code"
EXAMPLE_CONCEPT_QUERY="how does <domain concept> work"
EXAMPLE_SEMANTIC_QUERY_1="where do we handle <domain concept>?"
EXAMPLE_SEMANTIC_QUERY_2="how does the <subsystem> pipeline work?"
```

#### b. Generate and deploy

```bash
cd cursor/
./generate-mdc.sh projects/<project>.vars /tmp/<project>-hindsight.mdc

for repo in ~/go/src/github.com/<org>/*/; do
  mkdir -p "$repo/.cursor/rules"
  \cp /tmp/<project>-hindsight.mdc "$repo/.cursor/rules/hindsight-memory.mdc"
done
```

The template (`cursor/hindsight-memory.mdc.tmpl`) contains all the structural rules (recall gates, phase triggers, three-tier guidance, etc.). Only the project-specific variables differ.

### 10. Update Nightly Pipeline

In `nightly-learn.py`:
- Add `<project>-docs` and `<project>-issues` to `BANKS` list
- Add mental model refresh entries to `models_to_refresh`
- Add observability probes for new banks

In `report.py`:
- Add new banks to `collect_mental_model_stats()` bank list
- Add new bank coverage to `collect_ingestion_coverage()`
- Add new pgvector table to code chunk count queries

### 11. Verify End-to-End

```bash
# Check banks exist
curl -s http://localhost:8888/v1/default/banks | python3 -m json.tool

# Check launchd service
launchctl list | grep cocoindex

# Check CocoIndex logs
tail -20 ~/.hindsight/logs/<project>-cocoindex-stderr.log

# Check code embeddings
psql -h localhost -U hindsight -d hindsight \
  -c "SELECT count(*) FROM cocoindex.<project>_code_embeddings;"

# Test recall
curl -X POST http://localhost:8888/v1/default/banks/<project>-docs/memories/recall \
  -H 'Content-Type: application/json' \
  -d '{"query": "architecture overview", "max_tokens": 1024}'
```

## File Checklist

| File | Purpose |
|------|---------|
| `<project>-cocoindex-flows.py` | Ingestion (docs, issues, code) |
| `<project>-cocoindex-search.py` | Code search MCP server |
| `launchd/io.vectorize.cocoindex.<project>.plist` | macOS service |
| `cursor/projects/<project>.vars` | Template variables for cursor rule generation |
| `cursor/hindsight-memory.mdc.tmpl` | Shared template (do not edit per-project) |
| `cursor/generate-mdc.sh` | Generates .mdc from template + vars |
| Each repo's `.cursor/mcp.json` | Workspace-level MCP routing |

## Isolation Guarantees

- **Banks**: Fully separate Hindsight banks per project
- **Code index**: Separate pgvector tables (`code_embeddings` vs `<project>_code_embeddings`)
- **CocoIndex state**: Separate SQLite databases (`cocoindex.db` vs `<project>-cocoindex.db`)
- **MCP routing**: Workspace-level config ensures agents only see their project's data
- **Shared**: `cursor-memory` bank (behavioral corrections), Hindsight API instance, PostgreSQL
