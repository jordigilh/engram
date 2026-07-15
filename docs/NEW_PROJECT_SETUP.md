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

## Variants

The steps below describe the full pattern (dedicated docs bank + issues bank +
code table + own launchd service). Two lighter variants exist, both shipped
for real during the 2026-07-15 Engram-onboarding + kubernaut-operator/console
work (see [FINDINGS.md](FINDINGS.md)):

**No-issues-bank variant** — for a project with zero GitHub issues (e.g. this
repo, `engram`, which tracks bugs/decisions in `docs/FINDINGS.md` instead):
skip the `<project>-issues` bank entirely, skip the `issues_app` in the
CocoIndex flow file, and omit `issues_repos` from the project's
`PROJECT_CONFIGS` entry in both `nightly-learn.py` and `report.py` (both
files' `collect_ingestion_coverage()`/probe logic treat a missing
`issues_repos` key as "contributes nothing to the total," not an error —
verified by `tests/test_report.py::TestCollectIngestionCoverageProjectScoping::test_engram_project_contributes_nothing_to_issues_total`).

**Tag-scoped mental model variant** — for a sub-repo of an *already-onboarded*
project that wants its own focused recall/search view without the overhead of
a fully separate bank/table/pipeline (e.g. `kubernaut-operator`/
`kubernaut-console`, which are still ingested into the shared `kubernaut-docs`
bank / `code_embeddings` table, already tagged by repo at ingestion time). No
new bank, no new CocoIndex app, no new launchd service — just:
1. `create_mental_model` with a `tags: ["<repo>"]` filter, scoped to the
   existing shared bank (see `create-mental-models.py`'s `operator-architecture`/
   `console-architecture` entries).
2. An optional `repo` parameter on `cocoindex-search.py`'s `search_code()` /
   `cocoindex_search` MCP tool, which adds a `filepath LIKE '<repo>/%'` filter
   to scope code search the same way.
3. A hand-authored `.cursor/rules/hindsight-memory.mdc` that defaults to the
   repo's own tag/prefix for own-repo work, and explicitly drops the filter
   for cross-repo/upstream triage (see `cursor/operator-hindsight-memory.mdc`).

Use the full pattern below when a sub-repo's content volume, access pattern,
or lifecycle genuinely warrants isolation; use the tag-scoped variant when it
just needs a narrower lens on data that's already being ingested correctly.

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

> **Gotcha**: give the Postgres pool `coco.ContextKey(...)` a name unique
> across *every* flow file in this repo, not just this project's own file
> (e.g. `"<project>_repo_pg_pool"`, not the generic `"pg_pool"` that
> `cocoindex-flows.py` already uses). CocoIndex registers `ContextKey`s
> process-globally and raises `ValueError` on a same-name second
> registration — harmless in production (each flow file runs as its own
> `launchd` process), but it means the pytest suite will crash at collection
> time if it ever loads two flow files with colliding key names into one
> process. `engram-cocoindex-flows.py`'s `PG_POOL` (`"engram_repo_pg_pool"`)
> is the reference example; see `tests/test_engram_cocoindex_flows.py::TestModuleLoadsWithoutContextKeyCollision`
> for the regression guard.

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

If you're not ready to run the new project's ingestion live yet (e.g. mid
scale-down, or still validating the flow file with a manual `--mode backfill`
run first), it's fine to create the plist in `launchd/` and commit it without
this `load` step — `engram`'s own plist shipped this way initially. Nothing
else in this guide depends on the service actually being loaded.

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

If the generated rule needs hand-editing beyond what the template variables
cover (e.g. dropping a language-specific section, adding tag-scoped recall
guidance — see `cursor/engram-hindsight-memory.mdc`/`cursor/console-hindsight-memory.mdc`
for real examples), register the canonical/deployed pair in
`check-rule-sync.py`'s `RULE_PAIRS` dict so drift-checking covers it:

```python
RULE_PAIRS: dict[str, tuple[Path, Path]] = {
    "global": (CANONICAL, DEPLOYED),
    "<project>": (
        REPO_ROOT / "cursor" / "<project>-hindsight-memory.mdc",
        HOME / "go" / "src" / "github.com" / "<org>" / "<repo>" / ".cursor" / "rules" / "hindsight-memory.mdc",
    ),
    ...
}
```

`python3 check-rule-sync.py` (no `--pair`) checks every registered pair;
`--pair <project>` checks just one.

### 10. Update Nightly Pipeline

In `nightly-learn.py`:
- Add `<project>-docs` and `<project>-issues` to `BANKS` list
- Add a new entry to `PROJECT_CONFIGS[<project>]` with `banks`, `mental_models`, `probes`, `recall_banks`, `log_suffix`
- Add mental model refresh entries to `models_to_refresh`
- Add observability probes for new banks
- **Add `workspace_prefixes`** to the new `PROJECT_CONFIGS` entry (see 10a — easy to miss, since nothing errors if you skip it)

In `report.py`:
- Add new banks to `collect_mental_model_stats()` bank list
- Add new bank coverage to `collect_ingestion_coverage()`
- Add new pgvector table to code chunk count queries

### 10a. Scope Transcript-Derived Analytics Per Project (do not skip)

**Why this matters:** Bank/table/CocoIndex isolation (above) only covers *ingested content*
(docs, issues, code). It does **not** automatically isolate the nightly report's
*session analytics* — `effectiveness` (session distribution, weekly trend, recall
session stats, token signals, exploration efficiency) and `mcp_usage` (raw MCP call
counts/hit-rates). Those are derived from Cursor agent transcripts and an MCP hook
log that span **every** workspace on the machine, not just this project's repos.

If you add a new project without scoping these, its nightly report will silently
embed byte-identical global stats instead of project-specific ones — the numbers
look plausible (non-zero, well-formed) so this is easy to miss for weeks. This
happened when DCM was first added: both `kubernaut` and `dcm` nightly reports had
identical `effectiveness` blocks because `find_recent_transcripts()` had no
per-project filter.

To scope correctly:

1. Add `"workspace_prefixes": ["Users-jgil-go-src-github-com-<org>-<repo-prefix>"]`
   to the project's `PROJECT_CONFIGS` entry in `nightly-learn.py`. This should match
   the `~/.cursor/projects/<name>/` directory name(s) for the project's repos —
   run `ls ~/.cursor/projects/ | grep <org>` to find the actual prefix once at least
   one repo has been opened in Cursor.
2. `find_recent_transcripts(hours, workspace_prefixes=...)` filters transcripts by
   that prefix before they're used for effectiveness analysis. The **retain/corrections
   pipeline stays unfiltered on purpose** (`cursor-memory` is an intentionally shared,
   cross-project bank for coding-hygiene lessons) — only pass `workspace_prefixes`
   into the effectiveness/analytics call sites, not the retain call sites.
3. `mcp_usage` scoping additionally requires the `afterMCPExecution` hook
   (`~/.cursor/hooks/log-mcp-calls.sh`) to tag each logged call with a `project_dir`
   field (derived from `transcript_path` in the hook payload) — `analyze_mcp_effectiveness()`
   filters on that field. This field is only present on log lines written *after* the
   hook was updated to record it; older lines are silently excluded from scoped views.
4. Verify by comparing the `effectiveness` block across two projects' daily JSON
   reports (`~/.hindsight/logs/<date>.json` vs `<date>-dcm.json`) — they should differ,
   not match byte-for-byte.

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

- **Banks**: Fully separate Hindsight banks per project (full variant); or a
  shared bank filtered by `tags` at recall/mental-model-creation time
  (tag-scoped variant — see Variants above)
- **Code index**: Separate pgvector tables (`code_embeddings` vs `<project>_code_embeddings`);
  or the shared table filtered by `repo`-prefixed `filepath` at search time
  (tag-scoped variant)
- **CocoIndex state**: Separate SQLite databases (`cocoindex.db` vs `<project>-cocoindex.db`)
  (full variant only — the tag-scoped variant adds no new CocoIndex app at all)
- **MCP routing**: Workspace-level config ensures agents only see their project's data
- **Nightly analytics**: `effectiveness` and `mcp_usage` are isolated per project **only if**
  `workspace_prefixes` is set on the `PROJECT_CONFIGS` entry (step 10a) — this is not
  automatic and does not fail loudly if skipped
- **GitHub issues/PRs totals**: scoped per project via `PROJECT_CONFIGS[project]["issues_repos"]`
  in both `nightly-learn.py` and `report.py` — a project with no `issues_repos` key
  (e.g. the no-issues-bank variant) simply contributes nothing to any total, rather
  than defaulting to one hardcoded repo (the pre-2026-07-15 behavior; see FINDINGS.md)
- **Shared**: `cursor-memory` bank (behavioral corrections), Hindsight API instance, PostgreSQL
