# New Project Setup Guide

Use this checklist to add a project to Engram. It covers the current
configuration-driven deployment; architecture history and operational detail
belong in the linked guides.

## Choose a scope

| Scope | Add | Do not add |
| --- | --- | --- |
| **Full project** | Dedicated docs/issues banks, code table, CocoIndex flow, search server, and gateway route | Nothing from an already-ingested project unless it is intentionally shared |
| **Existing project family** | Tags, a focused mental model, and optional repo-scoped code search | A second bank, flow, or service when the existing index is sufficient |
| **Ingestion-only** | Docs/issues flow and deployment paths | Serena, client configuration, or a code index for repos nobody opens |
| **Jira-backed issues** | A narrow Jira scope and Keychain-backed authentication | A whole-project Jira import when only one epic/workstream is needed |

Use the full pattern only when the project's content volume, access pattern, or
lifecycle needs isolation. For issue credentials, Jira scope, backfills, and
polling, see [Issue Ingestion](ISSUE_INGESTION.md).

## Prerequisites

- Complete the platform setup in [INSTALL.md](INSTALL.md), or
  [INSTALL-linux.md](INSTALL-linux.md) on Linux.
- Hindsight is healthy on `localhost:8888`; PostgreSQL/pgvector is available on
  the DSN in the deployment config.
- `~/.engram/venv/` contains the installed Engram package and CocoIndex.
- `gh auth status` succeeds when GitHub issues or pull requests are in scope.
- Podman is installed if the packaged gateway runtime will be used.
- Keep credentials and runtime secrets in `~/.engram/config.env`. Keep paths,
  repositories, branches, workspace prefixes, index/table settings, and RCA
  settings in `~/.engram/projects.toml`. Neither populated file belongs in Git.

## Prepare source checkouts

Create or reuse stable local checkouts for every repository that the flow will
read. Forking is optional; ingestion only needs a readable checkout and an
authenticated tracker client.

```bash
mkdir -p ~/go/src/github.com/<org>
gh repo clone <org>/<repo> ~/go/src/github.com/<org>/<repo>
```

If the deployment uses detached branch mirrors, keep their definitions outside
the repository:

```bash
cp watch-mirrors-config.example.sh ~/.engram/watch-mirrors-config.sh
$EDITOR ~/.engram/watch-mirrors-config.sh
ENGRAM_WATCH_MIRRORS_CONFIG=~/.engram/watch-mirrors-config.sh \
  ./setup-watch-mirrors.sh
```

Add the resulting paths and branch choices to the project's `projects.toml`
table. The mirror service and refresh workflow are described in
[CocoIndex Operations](COCOINDEX.md).

## Add the project adapters

For a full project, copy the closest generic modules and adapt them to the
project's sources:

| File | Responsibility |
| --- | --- |
| `src/engram/flows/<project>.py` | Docs, issues, code, and optional transcript apps |
| `src/engram/search/<project>.py` | Hybrid/structural code search |
| `pyproject.toml` | `engram-flows-<project>` and `engram-search-<project>` entry points |
| `launchd/io.vectorize.cocoindex.<project>.plist` | Continuous flow service |
| `tests/` | Flow, search, and configuration regression coverage |

Start from `src/engram/flows/engram.py` and
`src/engram/search/engram.py`, not from a project-specific implementation.

Each adapter must use:

- project-specific Hindsight bank names;
- a project-specific `code_table` and CocoIndex state database;
- paths and repository lists loaded from `projects.toml`;
- a unique CocoIndex `ContextKey` for its Postgres pool; and
- the project's language and file patterns.

Omit the issues app and issues bank when the project has no issue/decision
corpus. A tag-scoped sub-repo needs none of these new modules; add only the
scope-specific model/rule/search configuration described in the existing flow.

See [CocoIndex Operations](COCOINDEX.md) for flow behavior and
[Architecture](README.md#hindsight-vs-cocoindex-vs-serena-division-of-labor)
for the division between memory, search, and code intelligence.

## Configure the deployment

Copy the canonical template once, then merge the new project into the existing
file rather than replacing another deployment's settings:

```bash
mkdir -p ~/.engram
test -e ~/.engram/projects.toml || \
  cp docs/projects.toml.example ~/.engram/projects.toml
$EDITOR ~/.engram/projects.toml
```

At minimum, add a project table and its paths. A full project usually needs:

```toml
[projects.<project>]
banks = ["cursor-memory", "<project>-docs", "<project>-issues"]
recall_banks = ["hindsight", "<project>-docs", "<project>-issues", "<project>-code"]
code_bank = "<project>-code"
code_table = "<project>_code_embeddings"
issues_repos = ["<org>/<repo>"]
workspace_prefixes = ["Users-<user>-go-src-github-com-<org>-<repo>"]
release_lines = ["v1.0"] # omit when the project has no release routes

[projects.<project>.paths]
repo_dir = "~/go/src/github.com/<org>/<repo>"
docs_dir = "~/go/src/github.com/<org>/<docs-repo>/docs"
cocoindex_db = "~/.engram/<project>-cocoindex.db"
```

For multi-repository projects, use `[[projects.<project>.repositories]]`
entries and add each source path under `[projects.<project>.paths]`. Use
`pr_repos` for a GitHub-PR-only flow. Add `jira_*`, `jira_limit`, or a tracked
Jira-key path only when the selected flow supports them.

For Kubernaut RCA, add only the deployment-specific `rca_project`,
`rca_repository`, `rca_projects`, database, and must-gather settings. See
[Kubernaut RCA MCP](KUBERNAUT_RCA_MCP.md) for the route and branch policy.

The project table is the source of truth for ingestion, search, coverage, and
RCA. Do not hardcode project metadata in Python, repository-path environment
variables, or committed plists. Keep normal credentials and runtime secrets in
`config.env`; Jira tokens are the documented Keychain exception and must not be
copied into either file.

## Create banks and models

Create only the banks used by the selected scope. `cursor-memory` is shared;
project docs and issues banks are isolated.

```bash
curl -fsS -X PUT "http://localhost:8888/v1/default/banks/<project>-docs" \
  -H 'Content-Type: application/json' \
  -d '{"description":"<Project> documentation and architecture"}'

# Omit this bank for an ingestion scope without issues.
curl -fsS -X PUT "http://localhost:8888/v1/default/banks/<project>-issues" \
  -H 'Content-Type: application/json' \
  -d '{"description":"<Project> issues, pull requests, and decisions"}'

curl -fsS -X PATCH "http://localhost:8888/v1/default/banks/<project>-docs/config" \
  -H 'Content-Type: application/json' \
  -d '{"updates":{"retain_extraction_mode":"chunks","retain_chunk_size":800}}'
# Omit the next command when issues are out of scope.
curl -fsS -X PATCH "http://localhost:8888/v1/default/banks/<project>-issues/config" \
  -H 'Content-Type: application/json' \
  -d '{"updates":{"retain_extraction_mode":"chunks","retain_chunk_size":800}}'
```

Keep new banks in the default LLM-free `chunks` mode unless a deliberate cost
decision enables extraction or consolidation. Local embeddings still support
recall; mental-model synthesis is a separate, on-demand operation.

List the model IDs and probes in `projects.toml`:

```toml
[projects.<project>.mental_models]
<project>-docs = ["<project>-architecture", "<project>-api-contracts"]
<project>-issues = ["active-priorities", "known-bugs"]
cursor-memory = ["<project>-workflow-preferences"]

[[projects.<project>.probes]]
bank = "<project>-docs"
query = "<important architecture question>"
```

Add the corresponding model definitions to
`src/engram/maintenance/create_mental_models.py`, then create and refresh the
configured models:

```bash
~/.engram/venv/bin/python3 -m engram.maintenance.create_mental_models
```

## Install, backfill, and start the flow

Install the package after adding the modules and entry points:

```bash
uv pip install --python ~/.engram/venv/bin/python -e .
```

Backfill a new project before relying on live file watching. A cold live scan
can record fingerprints without populating an empty code table.

```bash
~/.engram/with-config-env.sh \
  ~/.engram/venv/bin/engram-flows-<project> \
  --mode backfill --apps docs code issues
```

Omit `issues` when it is not configured. For GitHub/Jira-specific backfill and
polling, follow [Issue Ingestion](ISSUE_INGESTION.md).

After the backfill is healthy, render a project-specific plist based on the
closest existing `launchd/io.vectorize.cocoindex.*.plist` template. It should
invoke the installed console script through `~/.engram/with-config-env.sh` and
load deployment paths from `projects.toml`:

```bash
sed "s|__HOME__|$HOME|g" \
  launchd/io.vectorize.cocoindex.<project>.plist \
  > "$HOME/Library/LaunchAgents/io.vectorize.cocoindex.<project>.plist"

launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.vectorize.cocoindex.<project>.plist"
```

Keep the flow in `live` mode through launchd after the backfill.

If the CocoIndex state database is lost while its PostgreSQL tables remain,
drop the project's tables and their managed indexes/triggers before
re-backfilling. Do not delete tracking state alone.

## Expose one gateway route

The client should see one Engram MCP route. The gateway owns the Hindsight,
CocoIndex, Serena, and optional project-specific connections.

The recommended deployment is the stateless runtime image:

1. Copy `docs/runtime-instances.toml.example` to
   `~/.engram/runtime/instances.toml`.
2. Replace `my-project` and backend URLs with endpoints reachable from the
   container; remove unused backends.
3. Start the image on `127.0.0.1:8896`.

See [Runtime Image](RUNTIME_IMAGE.md) for the command and native-host
alternative. For OpenCode/OpenChamber, configure one explicit
`mcp.servers.engram` route and the global plugin as described in
[OpenCode Integration](OPENCODE.md). Do not register Hindsight, CocoIndex, and
Serena as separate client servers.

### Code intelligence

For a language-aware code backend, use Serena through the gateway. The language
server is selected by the project adapter; Rust projects additionally need a
local `rust-analyzer`. The OpenCode guide covers mappings, release routes, and
worktree-index policy.

## Optional integrations

- **Shared repository family:** use the shared-daemon and multiplex setup only
  when multiple active repos make separate processes a real resource problem.
  Start with the simpler per-project gateway route; see
  [OpenCode Integration](OPENCODE.md).
- **Correction enforcement:** run `bash hooks/install.sh <repo>` when the
  project needs the optional plan/contradiction hooks.
- **Self-healing Git hooks:** install the generic or family variant from
  [`git-hooks/README.md`](../git-hooks/README.md). Use the family variant only
  for a shared long-lived language-server daemon.

## Verify

```bash
curl -fsS http://localhost:8888/health
curl -fsS http://localhost:8888/v1/default/banks/<project>-docs
launchctl print "gui/$(id -u)/io.vectorize.cocoindex.<project>"
tail -20 "$HOME/.engram/logs/cocoindex-<project>-stderr.log"
psql -h localhost -U hindsight -d hindsight \
  -c "select count(*) from cocoindex.<project>_code_embeddings;"
```

Then verify each active route:

- recall a known document and, if configured, a known issue;
- run one code-search query and one real Serena symbol/reference lookup;
- call `tools/list` through `http://127.0.0.1:8896/mcp/<project>`;
- confirm the project prefix in `~/.cursor/projects/` matches
  `workspace_prefixes` and that a project report contains only its own
  transcript/MCP analytics.

## Repository and deployment checklist

Commit only repository changes:

- flow and search modules, if this is a full project;
- console-script and launchd templates;
- model definitions, rule/template changes, and tests;
- documentation updates.

Keep these deployment-local:

- `~/.engram/config.env` — credentials and runtime secrets;
- `~/.engram/projects.toml` — paths, repositories, branches, workspace
  prefixes, index/table settings, and RCA configuration;
- `~/.engram/runtime/*.toml` — populated gateway registries;
- `~/.engram/opencode.json` — client route and worktree policy;
- `~/.engram/watch-mirrors-config.sh` — mirror definitions;
- Jira Keychain entries and tracked-key files.

## Detailed references

- [Installation](INSTALL.md) / [Linux installation](INSTALL-linux.md)
- [CocoIndex Operations](COCOINDEX.md)
- [Issue Ingestion](ISSUE_INGESTION.md)
- [OpenCode and OpenChamber](OPENCODE.md)
- [Runtime Image](RUNTIME_IMAGE.md)
- [Kubernaut RCA MCP](KUBERNAUT_RCA_MCP.md)
- [Architecture and division of labor](README.md)
- [Self-healing Git hooks](../git-hooks/README.md)
