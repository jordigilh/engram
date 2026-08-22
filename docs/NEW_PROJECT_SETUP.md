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

> **2026-08-21 update**: the four backend servers below (`hindsight-docs`,
> `hindsight-issues`, `cocoindex-code`, `serena`) are now fronted by one
> aggregating gateway, `engram-gateway` (`src/engram/pipeline/
> engram_gateway.py`, supervised by `launchd/io.vectorize.engram-gateway.plist`)
> instead of each getting its own `.cursor/mcp.json` entry — see
> `docs/findings/2026-08.md`'s 2026-08-21 rollout entry for the full
> rationale (recurring Cursor MCP client flakiness with 4 separate
> connections per repo) and the current registry of every onboarded repo.
> The steps below (dedicated banks, pgvector table, CocoIndex flow, launchd
> service) are all still accurate and still what you set up per new
> project — only the *last* step changes: instead of adding 3-4 raw server
> entries to `.cursor/mcp.json`, add one entry to
> `build_project_registry()` in `engram_gateway.py` (with a test in
> `tests/test_engram_gateway.py`) and point the new repo's
> `.cursor/mcp.json` at `http://127.0.0.1:8896/mcp/<project>` instead.

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

**No-issues-bank variant** — for a project with no incident/decision issue
tracking to ingest (e.g. this repo, `engram`, which tracks bugs/decisions in
`docs/FINDINGS.md` instead; as of 2026-07-29 it does use a small number of
GitHub issues, but only for forward-looking enhancement proposals, a
separate and non-overlapping use case that still doesn't need an
`<project>-issues` bank): skip the `<project>-issues` bank entirely, skip
the `issues_app` in the
CocoIndex flow file, and omit `issues_repos` from the project's
`PROJECT_CONFIGS` entry in both `engram.pipeline.nightly_learn` and
`engram.maintenance.report` (both modules' `collect_ingestion_coverage()`/probe logic treat a missing
`issues_repos` key as "contributes nothing to the total," not an error —
verified by `tests/test_report.py::TestCollectIngestionCoverageProjectScoping::test_engram_project_contributes_nothing_to_issues_total`).

**Tag-scoped mental model variant** — for a sub-repo of an *already-onboarded*
project that wants its own focused recall/search view without the overhead of
a fully separate bank/table/pipeline (e.g. `kubernaut-operator`/
`kubernaut-console`, which are still ingested into the shared `kubernaut-docs`
bank / `code_embeddings` table, already tagged by repo at ingestion time). No
new bank, no new CocoIndex app, no new launchd service — just:
1. `create_mental_model` with a `tags: ["<repo>"]` filter, scoped to the
   existing shared bank (see `engram.maintenance.create_mental_models`'s
   `operator-architecture`/`console-architecture` entries).
2. An optional `repo` parameter on `engram.search.kubernaut`'s `search_code()` /
   `cocoindex_search` MCP tool, which adds a `filepath LIKE '<repo>/%'` filter
   to scope code search the same way.
3. A hand-authored `.cursor/rules/hindsight-memory.mdc` that defaults to the
   repo's own tag/prefix for own-repo work, and explicitly drops the filter
   for cross-repo/upstream triage (see `cursor/operator-hindsight-memory.mdc`).

Use the full pattern below when a sub-repo's content volume, access pattern,
or lifecycle genuinely warrants isolation; use the tag-scoped variant when it
just needs a narrower lens on data that's already being ingested correctly.

**Jira-scoped issues variant** — for a project whose issue/decision tracking
lives in Jira rather than GitHub issues, and where only a narrow slice of
that tracker (one epic, not the whole project) is in scope. First shipped
2026-08-13 for `rhdh-plugins` (epic RHIDP-15270 "AI Catalog Graduated
Visibility Permissions" + its child stories, out of a monorepo with 23
workspaces and a tracker with 166+ open issues project-wide). Differences
from the full pattern:
1. Step 1 (fork/clone) is usually just a single `git clone` of one existing
   checkout, not an org-wide fork loop — the target is often a single
   monorepo, not a multi-repo org.
2. Step 4's `issues_app` in the CocoIndex flow authenticates against Jira's
   REST API instead of `gh`. Shell out to `security find-generic-password`
   to reuse the same Keychain entry the `jira` CLI already has configured
   (see "Jira authentication" gotcha below) rather than prompting for a new
   token or expecting one in an env var — do **not** invoke the `jira` CLI
   itself as a subprocess for ingestion; its JQL/output flags are meant for
   interactive use and are awkward to script reliably (e.g. it rejects a
   trailing `ORDER BY` in some query forms, and silently mis-scopes cross-
   project queries unless `-p <PROJECT>` is explicit — both hit during the
   `rhdh-plugins` spike). Call the REST API directly instead, and flatten
   Jira's Atlassian Document Format (ADF) rich-text fields to plain text
   before ingestion (see `engram.flows.rhdh_plugins._adf_to_text`).
3. Scope the JQL to the target epic and its children explicitly (e.g.
   `parent = <EPIC> OR key = <EPIC> order by created asc`, see
   `engram.flows.rhdh_plugins._fetch_epic_and_children`), not a broad
   `project = <PROJECT>` — the latter would pull in every issue in the
   tracker, defeating the whole point of narrow-scope onboarding. No upper
   result `LIMIT` is needed here (unlike koku's whole-project ingestion,
   which caps via `KOKU_JIRA_LIMIT`) since one epic's subtree is expected to
   be a handful of issues, not thousands.
4. Omit `issues_repos` from the project's `PROJECT_CONFIGS` entry in both
   `engram.pipeline.nightly_learn` and `engram.maintenance.report`, same as
   the no-issues-bank variant above — `issues_repos` specifically means
   "GitHub repos to total issues/PRs across via `gh`," which doesn't apply
   to a Jira-sourced bank. The `<project>-issues` Hindsight bank and its
   mental model(s) still exist and still work normally for `recall`; only
   the GitHub-specific totals in `report.py` are skipped.
5. Also narrow the **code** app's scope to match the epic's actual footprint
   (e.g. one workspace/package directory inside a monorepo, not every
   package) — a Jira-scoped issues bank paired with a whole-monorepo code
   index would defeat the same narrow-scope goal from the code side instead.

> **Jira authentication gotcha**: don't require a separate Jira API token
> setup for ingestion if the machine already has the `jira` CLI
> (`github.com/ankitpokhrel/jira-cli` or similar) configured and
> authenticated — its token lives in the macOS Keychain under a
> predictable service/account name. Read it with the same `-a <account>
> -s <service>` pair the CLI itself uses (e.g.
> `security find-generic-password -a jira-cli -s jira-cloud-api-token -w` —
> see `engram.flows.rhdh_plugins._jira_token`) instead of asking the user
> for a fresh token; this was the approach the user explicitly chose over
> prompting for new credentials during the `rhdh-plugins` onboarding. Also
> prefer calling Jira's REST API (`/rest/api/3/search/jql`) directly with
> that token over shelling out to the `jira` CLI for ingestion —
> `jira-cli`'s `--paginate` has a real bug against Jira Cloud's newer
> `/search/jql` endpoint (see koku's `_jira_token()` docstring for the full
> history), and its query/output flags are meant for interactive use, not
> scripted ingestion.

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

Create `src/engram/flows/<project>.py` adapted from `src/engram/flows/kubernaut.py`:

- Three apps: docs, issues, code
- Banks: `<project>-docs`, `<project>-issues`
- pgvector table: `cocoindex.<project>_code_embeddings` (isolated from other projects)
- Separate CocoIndex state DB: `~/.hindsight/<project>-cocoindex.db`
- Environment variables prefixed with `<PROJECT>_*`

Add an `engram-flows-<project> = "engram.flows.<project>:main"` entry under
`[project.scripts]` in `pyproject.toml`, then re-run
`uv pip install --python ~/.hindsight/venv/bin/python -e .` to generate the
console script.

> **Gotcha**: give the Postgres pool `coco.ContextKey(...)` a name unique
> across *every* flow module in this package, not just this project's own
> module (e.g. `"<project>_repo_pg_pool"`, not the generic `"pg_pool"` that
> `engram.flows.kubernaut` already uses). CocoIndex registers `ContextKey`s
> process-globally and raises `ValueError` on a same-name second
> registration — harmless in production (each flow module runs as its own
> `launchd` process), but it means the pytest suite will crash at collection
> time if it ever loads two flow modules with colliding key names into one
> process. `engram.flows.engram`'s `PG_POOL` (`"engram_repo_pg_pool"`)
> is the reference example; see `tests/test_engram_cocoindex_flows.py::TestModuleLoadsWithoutContextKeyCollision`
> for the regression guard.

### 5. Create Code Search Server

Create `src/engram/search/<project>.py` adapted from `src/engram/search/kubernaut.py`:

- Queries `cocoindex.<project>_code_embeddings` table
- MCP server name: `<project>-code`
- Tool name: `<project>_code_search`
- Same hybrid search (dense + BM25 + RRF fusion)

Add a matching `engram-search-<project> = "engram.search.<project>:main"`
entry under `[project.scripts]` alongside the flows one from step 4, then
re-run the same `pip install -e .` (it's idempotent to run twice).

### 6. Create launchd Service

Create `launchd/io.vectorize.cocoindex.<project>.plist`:

- Runs the `engram-flows-<project>` console script (from step 4) directly in
  live mode via `~/.hindsight/with-config-env.sh` (the shared wrapper that
  sources `~/.hindsight/config.env` for secrets/URLs at runtime instead of
  hardcoding them in the plist) — **not** a `~/.hindsight/<project>-
  cocoindex-flows.py` symlink. Pre-Phase-8 (before 2026-08-12) every
  project's plist went through such a symlink because there was no
  installed package to point `ProgramArguments` at yet; that phase is done
  for every existing project (see docs/findings/2026-08.md's Phase 8 entry)
  and no new project should reintroduce the symlink pattern. Use an existing
  plist (e.g. `launchd/io.vectorize.cocoindex.rhdh-plugins.plist`) as the
  template, not an older one predating Phase 8.
- Environment variables for all repo paths
- Separate log files: `~/.hindsight/logs/cocoindex-<project>-{stdout,stderr}.log`
  (project name last, not first — matches every existing plist's actual
  `StandardOutPath`/`StandardErrorPath`, e.g. `cocoindex-koku-stderr.log`)
- KeepAlive: true

Install and start (no flow/search symlinking needed — the console scripts
from steps 4/5 are already on `PATH` inside `~/.hindsight/venv/bin/`):

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

### 7. Choose Your Code-Intelligence Backend

> See [docs/README.md's Division of Labor](README.md#hindsight-vs-cocoindex-vs-serena-division-of-labor)
> for how Serena's role here differs from Hindsight (memory) and CocoIndex
> (search) — this section is setup instructions only, not a conceptual
> overview.

Every onboarded project needs a code-intelligence MCP server so agents get
real symbol lookup/find-references/diagnostics instead of grepping for
identifiers. **[Serena](https://github.com/oraios/serena)** (an LSP-wrapping
MCP server) is the default backend for every language this project has
onboarded so far — it wraps the language's real LSP (`gopls` for Go,
`pyright` for Python, `rust-analyzer` for Rust,
`typescript-language-server` for TypeScript) behind one consistent MCP tool
surface, so the same `find_symbol`/`find_referencing_symbols`/
`get_diagnostics_for_file` tools work regardless of language.

Add a `serena` entry to the project's `.cursor/mcp.json` (step 8 below):

```json
"serena": {
  "command": "/Users/jgil/.local/bin/uvx",
  "args": [
    "--from", "git+https://github.com/oraios/serena",
    "serena", "start-mcp-server",
    "--project", "${workspaceFolder}",
    "--context", "ide",
    "--add-mode", "no-memories",
    "--open-web-dashboard", "false"
  ],
  "type": "stdio"
}
```

Plus a per-repo `.serena/project.yml` — Serena's project registration is
keyed by absolute path, so unlike `.cursor/mcp.json` (which some repo
families share via a symlinked template, see step 8's kubernaut-family
gotcha) this file can never be shared across repos, even siblings in the
same family:

```yaml
language_servers: [<go|python|rust|typescript>]
```

And a `.gitignore` entry (same wording used for every onboarded repo so far
— this reflects that Serena is a personal trial, not yet a team decision):

```
# Serena MCP trial (project config/cache/logs; not yet a team decision)
.serena/
```

> **Prerequisite per language, verified during real onboardings**:
> - **Go**: no extra install — Serena's `solidlsp` layer auto-manages/caches
>   a `gopls` binary itself.
> - **Python**: no extra install — same auto-management, via `pyright`.
> - **Rust**: Serena does **not** auto-install `rust-analyzer` — it fails
>   fast with an explicit "Stop, do not attempt workarounds" message if it's
>   missing. Install via `brew install rust-analyzer` (or
>   `rustup component add rust-analyzer`). Also install `rustup` itself
>   (not just Homebrew's `rustc`/`cargo`) if the target repo pins a
>   `rust-toolchain.toml` channel newer than Homebrew's current formula —
>   rust-analyzer's background `cargo check`/flycheck fails diagnostics-only
>   in that case (symbol lookup/find-references are unaffected) until the
>   pinned toolchain is installed and `~/.cargo/bin` takes `PATH` priority
>   over Homebrew's own `rustc`/`cargo`.
> - **TypeScript**: no extra install — same auto-management as Go/Python, via
>   `typescript-language-server`. Spiked and confirmed working 2026-08-13
>   against `rhdh-plugins` (a 23-workspace Yarn/Node monorepo): both
>   `find_symbol` and `find_referencing_symbols` returned correct results.
>   One perf note, not a correctness issue: an unscoped whole-repo search can
>   be slow on first call in a large monorepo (cold index build), but is
>   cached and fast on repeat calls and with `relative_path` scoping — pass
>   `relative_path` to the target workspace/package when possible instead of
>   searching the whole monorepo every time.

**Known limitation (all languages)**: for symbols located inside certain
non-declaration contexts (e.g. Go's Ginkgo `var _ = Describe(...)` test
blocks, some Rust module-level statements), `find_referencing_symbols` can't
attribute a named containing symbol and falls back to file-level attribution
(`"kind": "File"`, `"name": None`) — the reference *location* itself is
still found correctly, just less precisely labeled. On large codebases or
very common identifiers, `find_referencing_symbols` can also time out
(~235s observed on a broad identifier in a large Rust repo) — every other
tool still works normally when this happens.

### 8. Configure Workspace-Level MCP

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
      "command": "/Users/jgil/.hindsight/venv/bin/engram-search-<project>",
      "type": "stdio",
      "env": {
        "COCOINDEX_PG_URL": "postgresql://hindsight:hindsight@localhost:5432/hindsight"
      }
    }
  }
}
```

The workspace-level config uses the same server **names** as kubernaut (`hindsight-docs`, `hindsight-issues`, `cocoindex-code`) but points to different backends. Cursor rules reference these server names, so the same `recall` calls work across projects.

> **Gotcha**: whether to commit this file depends on whether the repo is
> personal/single-machine or shared/multi-contributor. `engram` and
> `kubernaut-console` commit `.cursor/mcp.json` directly (only this machine
> ever clones them). Repos with real outside contributors (`kubernaut`,
> `kubernaut-v1.5`, `kubernaut-v1.6`, `kubernaut-operator`) instead gitignore
> it via a blanket `.cursor/*` in `.gitignore` (with `!.cursor/rules/` /
> `!.cursor/skills/` carved back out, but no exception for `mcp.json`) —
> because the file embeds this machine's absolute paths
> (`/Users/jgil/.hindsight/venv/bin/python3`, `/Users/jgil/.local/bin/uvx`),
> which would be wrong on every other contributor's machine if committed.
> An untracked, gitignored `.cursor/mcp.json` still survives ordinary
> `git checkout`/`git switch` between branches in the same working directory
> (checkout only adds/removes *tracked* files) — verified empirically
> 2026-08-03 in `kubernaut-operator`. It does **not** survive
> `git clean -fdx` (the `-x` pulls in ignored files) or a brand-new
> `git clone`/`git worktree add` of the repo elsewhere, since those only
> materialize tracked content — if a repo is ever missing this file, that's
> the likely cause; just redo this step rather than trying to make git
> remember a file it's deliberately excluding (see FINDINGS.md).

> **Gotcha (repo families sharing one physical file)**: a family of closely
> related repos (e.g. `kubernaut`/`kubernaut-v1.5`/`kubernaut-v1.6`/
> `kubernaut-operator`) can have every repo's `.cursor/mcp.json` be a
> filesystem symlink to one shared file under
> `~/.hindsight/cursor-mcp-templates/<family>.json`, so a config change (like
> adding step 7's `serena` entry) is one edit that cascades to every repo in
> the family instead of N separate edits. This is easy to miss when
> retrofitting an existing family — check with `readlink` before assuming a
> repo's `.cursor/mcp.json` is a plain, independent file.
>
> **Evolution (2026-08-13, shared HTTP daemons instead of one stdio process
> per window)**: for a large family (kubernaut-family: 6 repos), even with
> the symlinked-template gotcha above, opening N repos as N separate Cursor
> windows still spawns N `cocoindex-code` subprocesses and N `serena`+`gopls`
> subprocesses (loading the same ~855-package Go module N times). If that's
> a real resource concern, run `engram-search-<project>`/`serena
> start-mcp-server` once each as permanent `launchd` daemons
> (`--transport streamable-http`, fixed host/port) instead, and point the
> shared template's `cocoindex-code`/`serena` entries at
> `"type": "http"` + a fixed `http://127.0.0.1:<port>/mcp` URL instead of
> `command`/`stdio`. See `launchd/io.vectorize.cocoindex-code.kubernaut-family.plist`,
> `launchd/io.vectorize.serena.kubernaut-family.plist`, and
> `launchd/io.vectorize.serena-project-server.plist` for the concrete
> templates, and `docs/findings/2026-08.md`'s 2026-08-13 (same day, seventh
> and eighth follow-ups) entry for the full rationale and a real gotcha this
> surfaced: starting the shared `serena` daemon with a fixed `--project`
> silently disables the `activate_project` tool for every *other* repo in the
> family, so the daemon must start with **no** `--project` and
> `--add-mode query-projects` instead — an agent calls `activate_project`
> with its own repo's path to get full read+write, or `query_project` for a
> read-only peek at a different family member without switching. `serena
> start-project-server` (one instance, not per-repo) must also be running as
> a separate daemon for `query_project` to work at all. This pattern doesn't
> replace per-window `stdio` as the *default* for a newly onboarded, standalone
> project — only worth the added complexity once a family is large enough
> that N duplicate processes are a measurable resource concern.
>
> **On Linux**: same architecture, `systemd --user` units instead of
> `launchd` plists — see `docs/INSTALL-linux.md` step 9 and
> `systemd/engram-cocoindex-code-kubernaut-family.service` /
> `systemd/engram-serena-kubernaut-family.service` /
> `systemd/engram-serena-project-server.service` for the direct analogs of
> this section's 3 plists, including a real Postgres-reachability gotcha
> specific to Linux's containerized Hindsight deployment that macOS doesn't
> have (step 9 documents the fix).
>
> **This still leaves one gap**, closed by step 8a below: the shared `serena`
> daemon has exactly one process-global "active project" at a time, so only
> whichever family repo last called `activate_project` gets full read+write —
> every other repo is stuck on read-only `query_project` until it "steals"
> activation back (and two windows on two different repos genuinely race for
> it). If your family is small/rarely-concurrent enough that "read-only for
> whichever repo isn't currently active" is acceptable, stop here. If you
> want every repo to get full read+write all the time, do step 8a too.

### 8a. (Optional, Repo Families Only) Give Every Family Repo Full Read+Write via `engram-serena-multiplex`

If you're setting up a **family of repos under one org that should share one
Serena instance** (the exact scenario this section exists for), you will hit
the gap described above as soon as more than one repo is actively being
worked on. `engram-serena-multiplex` (`src/engram/pipeline/serena_multiplex.py`)
is the fix: a small daemon that sits in front of the shared `serena` daemon
from step 8 and gives every family repo its own fixed HTTP mount
(`http://127.0.0.1:<multiplex-port>/mcp/<project-name>`). Every tool call
arriving on a mount is preceded — transparently, serialized behind one lock —
by an `activate_project(<that mount's own project>)` call against the shared
upstream, so every repo gets full read+write and concurrent calls from
different repos' windows are safely serialized instead of racing. See
`docs/findings/2026-08.md`'s 2026-08-13 (same day, ninth follow-up) entry for
the full design rationale, including a real bug hit and fixed along the way
(an earlier version built on `fastmcp`'s `Client`/`create_proxy` crashed the
shared upstream daemon — a known open bug class in the `mcp`/`fastmcp`
SDKs' SSE-reconnect handling; the shipped version is a minimal one-shot
`httpx` POST relay that avoids it entirely).

**When to use this**: you already did step 8's shared-daemon setup, your
family has 2+ repos that get worked on concurrently (even just "you, in two
Cursor windows"), and read-only access to whichever repo *isn't* currently
active is not acceptable. **When to skip it**: a single-repo project (there's
nothing to multiplex), or a family where only one repo is ever actively
edited at a time (step 8's plain shared daemon is simpler and sufficient).

1. **Prerequisite**: step 8's shared `serena` daemon must already be running
   with no fixed `--project` and `--add-mode query-projects` (as documented
   above) — the multiplex calls `activate_project` against that same daemon,
   so it needs `activate_project` to actually be available.

2. **Add a launchd daemon** for the multiplex itself, one per family (not
   per repo) — see `launchd/io.vectorize.serena-multiplex.<family>.plist`
   for the template. Pick a fixed port distinct from the shared `serena`
   daemon's port (e.g. `serena` on 8892, multiplex on 8893):

   ```xml
   <key>ProgramArguments</key>
   <array>
       <string>__HOME__/.hindsight/venv/bin/engram-serena-multiplex</string>
       <string>--host</string><string>127.0.0.1</string>
       <string>--port</string><string>8893</string>
       <string>--upstream-url</string><string>http://127.0.0.1:8892/mcp</string>
       <!-- one --project <name> per family repo, matching the names
            registered in ~/.serena/serena_config.yml, not repo paths -->
       <string>--project</string><string>my-repo-a</string>
       <string>--project</string><string>my-repo-b</string>
   </array>
   ```

3. **Give each repo its own `.cursor/mcp.json` `serena` entry**, pointed at
   its own mount:

   ```json
   "serena": { "type": "http", "url": "http://127.0.0.1:8893/mcp/<project-name>" }
   ```

   **This breaks the step-8 "one shared template symlinked by every family
   repo" trick for the `serena` entry specifically** — the URL is now
   per-repo, so a single template file can no longer serve every repo
   verbatim. Either give each repo its own small template file (one per
   project, everything else identical) or drop the symlink for just this key
   and hand-edit it per repo. `cocoindex-code`/`hindsight-docs`/
   `hindsight-issues` entries are unaffected (still identical across the
   family) and can stay shared.

4. **Update your family's git-hook restart script** (step 14) to also
   `launchctl kickstart -k` the multiplex daemon's label alongside `serena`
   and `cocoindex-code` — otherwise a `git checkout`/`pull` that changes
   files on disk won't refresh the multiplex's view of anything it caches
   (currently nothing beyond the active-project pointer, but keep it
   symmetric with the other two daemons for when that changes).

5. **Verify**: open two repos' mounts as two independent MCP sessions (or
   two real Cursor windows) and confirm each keeps reporting its own project
   via `get_current_config` even after the other activates a different one
   in between — that's the actual guarantee this buys you, not just "it
   responds to requests."

> **On Linux**: `systemd/engram-serena-multiplex-kubernaut-family.service`
> is the direct analog of this step's launchd plist — same
> `engram-serena-multiplex` console script, same `--project`/
> `--upstream-url` flags, just `systemctl --user enable --now
> <unit>.service` instead of `launchctl bootstrap`. See
> `docs/INSTALL-linux.md` step 9 for the full command sequence (bundled
> together with steps 2 and 3's plain shared-daemon units, since on Linux
> there's no reason to install one without the other).

### 9. Slim the Global MCP Config

The global `~/.cursor/mcp.json` should only contain servers that are truly
shared across every project on the machine — in practice, today, just
`hindsight` (the `cursor-memory` bank):

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

The code-intelligence backend (step 7) is **workspace-scoped, not global** —
each repo's own `.cursor/mcp.json` carries its own `serena` entry with
`--project ${workspaceFolder}`, since Serena's language server and index are
per-project state that a single global entry couldn't represent correctly
across repos in different languages anyway. Project-specific servers are
defined at workspace level and override global ones when names collide.

### 10. Create Cursor Rule

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

### 11. Update Nightly Pipeline

In `src/engram/pipeline/nightly_learn.py`:
- Add `<project>-docs` and `<project>-issues` to `BANKS` list
- Add a new entry to `PROJECT_CONFIGS[<project>]` with `banks`, `mental_models`, `probes`, `recall_banks`, `log_suffix`
- Add mental model refresh entries to `models_to_refresh`
- Add observability probes for new banks
- **Add `workspace_prefixes`** to the new `PROJECT_CONFIGS` entry (see 11a — easy to miss, since nothing errors if you skip it)

In `src/engram/maintenance/report.py`:
- Add new banks to `collect_mental_model_stats()` bank list
- Add new bank coverage to `collect_ingestion_coverage()`
- Add new pgvector table to code chunk count queries

### 11a. Scope Transcript-Derived Analytics Per Project (do not skip)

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
   to the project's `PROJECT_CONFIGS` entry in `src/engram/pipeline/nightly_learn.py`. This should match
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

### 12. Verify End-to-End

```bash
# Check banks exist
curl -s http://localhost:8888/v1/default/banks | python3 -m json.tool

# Check launchd service
launchctl list | grep cocoindex

# Check CocoIndex logs
tail -20 ~/.hindsight/logs/cocoindex-<project>-stderr.log

# Check code embeddings
psql -h localhost -U hindsight -d hindsight \
  -c "SELECT count(*) FROM cocoindex.<project>_code_embeddings;"

# Test recall
curl -X POST http://localhost:8888/v1/default/banks/<project>-docs/memories/recall \
  -H 'Content-Type: application/json' \
  -d '{"query": "architecture overview", "max_tokens": 1024}'

# Health-check the code-intelligence backend (step 7) against real code —
# do a couple of real find_symbol/find_referencing_symbols calls, not just
# the health-check CLI, which can land on a file with no top-level symbols
# (e.g. a Ginkgo test file) and report a false negative.
# NOTE: the target path is a positional argument, not a --project flag --
# `serena project health-check --project <path>` fails with an unrecognized-
# option error (verified 2026-08-13); `--help` confirms the positional form.
uvx --from git+https://github.com/oraios/serena serena project health-check /path/to/target-repo
```

### 13. Install the Deterministic Correction Enforcement Hooks (optional)

Recall and cursor rules are *advisory* — a model can always choose not to
call `recall`, and a summarized/compacted context can silently drop a rule's
instructions. The hook family in `hooks/` is the harness-enforced
alternative: Cursor hooks cannot be skipped by the model the way a rule file
can (see docs/findings/2026-08.md for the full design rationale and spike
history).

Three hooks, sharing one per-session marker:

- `hooks/detect-plan-kickoff.sh` (`beforeSubmitPrompt`) — detects Cursor's
  auto-continue "implement the plan" message, extracts the newly-confirmed
  plan's `overview` field plus the repo name (basename of the first
  workspace root), and caches both to a per-session marker file.
- `hooks/post-plan-hindsight-check.py` (`preToolUse`, matcher
  `Write|StrReplace|Shell|EditNotebook`) — on the first matched tool call
  after a plan is confirmed, consumes that marker and runs a real
  `contradiction_resolution.resolve()` check (in a subprocess under a hard
  45s wall-clock watchdog) against the target project's `cursor-memory`
  bank. A genuine contradiction hard-blocks the call with
  `permission: deny` and a `user_message` explaining the conflict; the
  model can then retry (same or revised) and pass through cleanly — this is
  a one-time speed bump per plan, not a permanent lock, because the check
  detects semantic conflict, not intent (see docs/findings/2026-08.md for a
  real false-positive example: a plan that *deliberately* superseded a
  stale convention looks identical to one that violates a still-valid one).
- `hooks/post-plan-checklist-reminder.py` (`postToolUse`, same matcher) —
  fires right after the enforcer, on the same tool call. If
  `~/.hindsight/review-checklists/<repo>.md` exists for the marker's repo,
  injects it via `additional_context` as a one-time reminder (e.g. a
  project's Pre-PR review checklist that reviewers keep having to repeat).
  No-ops (and is not even registered in `hooks.json`, see below) for repos
  without a checklist file — currently that's every kubernaut repo, and all
  dcm-project repos except `osac-service-provider`.

Install into a target repo:

```bash
cd /path/to/engram
bash hooks/install.sh /path/to/target-repo
```

This is idempotent (safe to re-run) and merges into any existing
`.cursor/hooks.json` rather than overwriting it. It also symlinks the hook
scripts into `~/.hindsight/hooks/` and the whole `hooks/review-checklists/`
directory into `~/.hindsight/review-checklists/` — the single stable
locations every onboarded repo's `hooks.json` points at, so the hooks keep
working even if engram's own checkout path ever changes (same rationale as
`chunking.py`'s symlink into `~/.hindsight/`, see step 4 above and
docs/INSTALL.md).

`install.sh` auto-detects kubernaut paths (any path containing
`/kubernaut`) and, for those, registers only the detector+enforcer pair —
the checklist reminder hook is not added to their `hooks.json` at all, since
the checklist feature is DCM-specific. To add a checklist for a future
(non-kubernaut) repo, just add `hooks/review-checklists/<repo>.md` and
commit it — no script changes needed, the enforcer and reminder both key off
file existence, not a hardcoded project list.

> **Gotcha**: the enforcer's/reminder's `command` in `hooks.json` must
> invoke `~/.hindsight/venv/bin/python3`, never a bare `python3`/`python` —
> the real `resolve()` call needs `litellm`/`vertexai`, which only exist in
> Hindsight's venv. `hooks/install.sh` gets this right automatically; if
> you ever hand-edit `hooks.json`, don't "simplify" the interpreter path.

> **Gotcha**: like `.cursor/mcp.json` (step 8), `hooks.json` embeds this
> machine's absolute paths and is gitignored via the same blanket
> `.cursor/*` pattern — it will not exist in a fresh clone/worktree until
> `hooks/install.sh` is re-run there.

> **Gotcha (checklist content security)**: checklist files are git-tracked
> inside engram's own repo, not committed to the target repo (e.g.
> `osac-service-provider`'s own contributors, most of whom don't use
> Engram, never see this file) and not stored as a bare untracked file
> under `~/.hindsight/` either — `git diff`/`git log` on
> `hooks/review-checklists/*.md` is the real integrity control against
> tampering. `post-plan-checklist-reminder.py`'s
> `is_safe_checklist_content()` is a cheap secondary sanity check (max
> length, no URLs, no obvious prompt-injection phrasing), not a
> replacement for reviewing checklist-file diffs.

Known scope limits (accepted, not bugs): a `Task` subagent's tool calls
carry a different `session_id` than its parent conversation, so
subagent-delegated plan implementation gets zero coverage from either the
enforcer or the reminder; the enforcer check only fires on the *first*
matched tool call per plan (by design — the marker is consumed immediately
unless a checklist file is deferring it, see above), not on every
subsequent one; and a `preToolUse` deny always deletes the marker
immediately, so a denied-then-retried plan never gets a checklist reminder
either (`postToolUse` never fires after a `preToolUse` deny).

### 14. Install Self-Healing Git Hooks (recommended)

Separate from step 13's correction-enforcement hooks, `git-hooks/` in this
repo holds **templates and ready-to-use scripts** for a family of plain git
hooks (`post-checkout`, `post-merge`, `reference-transaction`) — see
`git-hooks/README.md` for the full reference; this step is a summary.
`~/.hindsight/git-hooks/` is where you *install* (symlink or generate) them
per-machine — it does not come pre-populated; that directory is created by
this step, not shipped with engram. These hooks keep two things from
silently going stale as a repo's working tree changes underneath a running
Cursor session:

1. **Language-server staleness**: `gopls mcp` / `serena start-mcp-server`
   processes cache file state at startup. A `git checkout`/`pull`/`merge`/
   `reset`/`rebase` that rewrites files on disk without restarting these
   processes leaves them serving stale symbol/reference data. These hooks
   kill any matching stale process (matched by cwd for `gopls`, by
   `--project <toplevel>` for `serena`) so the next Cursor MCP call
   auto-respawns a fresh one.
2. **(kubernaut/dcm families only) `.cursor/mcp.json` template drift**: for
   repo families sharing one symlinked `.cursor/mcp.json` template (step 8's
   "repo families" gotcha), `post-checkout-cursor-mcp.sh`/
   `post-checkout-dcm-mcp.sh` also re-provision that symlink on checkout.

This gap was found and closed 2026-08-13: koku (3 clones), every
praxis-proxy repo (10 clones), and engram itself had never received this
rollout (only kubernaut-family and dcm-project had it), which was the
concrete, fixable half of Cursor repeatedly showing MCP servers as
"Disabled" across those projects (see docs/findings/2026-08.md's 2026-08-13
entry — the other half is a genuine Cursor UI stale-label bug with no
hook-side fix). A **generic** variant is checked into this repo at
`git-hooks/generic/` for exactly this case: same gopls/serena-restart +
self-provisioning behavior as the family variant, but deliberately skips
the `.cursor/mcp.json` symlink step, since koku/praxis-proxy/engram (and any
newly onboarded single-repo project like `rhdh-plugins`) use real,
non-symlinked `mcp.json` files per repo.

**2026-08-16 correction**: an earlier version of `post-checkout-generic-mcp.sh`
(and the dcm-family variant) self-provisioned `post-merge`/
`reference-transaction` from the kubernaut-family-*specific* scripts, which
restart the kubernaut-family shared serena daemon — completely irrelevant to
a generic/dcm repo's own per-repo gopls/serena, and it means routine git
operations across 30 unrelated repos were needlessly restarting
kubernaut-family's daemon (and never actually refreshing their own). Fixed
by adding dedicated `post-merge-generic-mcp.sh` /
`reference-transaction-generic-mcp.sh` (family-agnostic, per-repo restart
only) and correcting both `post-checkout-generic-mcp.sh` and
`post-checkout-dcm-mcp.sh` to self-provision those instead. See
`docs/findings/2026-08.md`, 2026-08-16 entry, for the full incident.

Install the generic variant (symlink, don't copy, so future fixes to the
shared script land in every repo without a re-run):

```bash
d=/path/to/target-repo
ln -sf /path/to/engram/git-hooks/generic/post-checkout-generic-mcp.sh "$d/.git/hooks/post-checkout"
ln -sf /path/to/engram/git-hooks/generic/post-merge-generic-mcp.sh      "$d/.git/hooks/post-merge"
ln -sf /path/to/engram/git-hooks/generic/reference-transaction-generic-mcp.sh "$d/.git/hooks/reference-transaction"
```

`post-merge` and `reference-transaction` are also self-provisioned by
`post-checkout` on its own next run if either is missing, so re-linking just
`post-checkout` after a fresh clone is normally enough — but link all three
explicitly for a brand-new onboarding rather than relying on that
self-healing to fire first. Use the **family** variant instead of generic
only if the new project *does* share either a symlinked `.cursor/mcp.json`
template or a long-lived shared HTTP MCP daemon with sibling repos (step 8's
family gotcha / step 8a) — see `git-hooks/README.md` for the family variant's
templated install (`git-hooks/family/*.sh.tmpl` + `git-hooks/generate-hooks.sh`
+ a real worked example at `git-hooks/families/kubernaut-family.vars`),
which also carries the `.cursor/mcp.json` template-selection logic the
generic variant deliberately omits.

> **Gotcha**: these are plain POSIX shell hooks in `.git/hooks/`
> (or the repo's `core.hooksPath` equivalent, if set), not Cursor
> `hooks.json` entries — they run for *any* git client (command line,
> Cursor's own git integration, etc.), not just tool calls the agent makes.
> Verify with a smoke test after installing: run
> `"$d/.git/hooks/post-checkout"` directly and confirm exit code 0, then
> check `post-merge`/`reference-transaction` got self-provisioned (or link
> them explicitly per above).

## File Checklist

| File | Purpose |
|------|---------|
| `src/engram/flows/<project>.py` | Ingestion (docs, issues, code) |
| `src/engram/search/<project>.py` | Code search MCP server |
| `launchd/io.vectorize.cocoindex.<project>.plist` | macOS service |
| `cursor/projects/<project>.vars` | Template variables for cursor rule generation |
| `cursor/hindsight-memory.mdc.tmpl` | Shared template (do not edit per-project) |
| `cursor/generate-mdc.sh` | Generates .mdc from template + vars |
| Each repo's `.cursor/mcp.json` | Workspace-level MCP routing |
| Each repo's `.serena/project.yml` | Per-repo Serena language-server registration (step 7); cannot be shared/symlinked, keyed by absolute path |
| `src/engram/pipeline/serena_multiplex.py` / `engram-serena-multiplex` | (Optional, step 8a) Gives every repo in a shared-Serena family full read+write instead of just whichever is "active" |
| `launchd/io.vectorize.serena-multiplex.<family>.plist` | (Optional, step 8a) macOS service for the multiplex daemon, one per family |
| `systemd/engram-{cocoindex-code,serena,serena-project-server,serena-multiplex}-kubernaut-family.service` | (Optional, steps 8/8a) Linux (`systemd --user`) analogs of the 4 shared-daemon launchd plists above — see `docs/INSTALL-linux.md` step 9 |
| `hooks/install.sh` | Installs the Deterministic Correction Enforcement hook family (optional, step 13) |
| Each opted-in repo's `.cursor/hooks.json` | Harness-enforced plan-kickoff detector + contradiction-check enforcer (+ checklist reminder for non-kubernaut repos) |
| `hooks/review-checklists/<repo>.md` | Per-repo PR review checklist content, injected by the checklist-reminder hook when present |
| `git-hooks/generic/*.sh` | Self-healing plain git hooks, single-repo variant: restart stale gopls/serena processes on checkout/merge/rebase/reset (recommended, step 14) |
| `git-hooks/family/*.sh.tmpl` + `git-hooks/generate-hooks.sh` + `git-hooks/families/*.vars` | Templated variant for repos sharing a long-lived HTTP MCP daemon: same restart behavior + re-provisions a shared `.cursor/mcp.json` template (optional, steps 8/8a/14) |
| `~/.hindsight/git-hooks/*.sh` | Per-machine install target: symlinks (generic) or `generate-hooks.sh` output (family) land here (step 14) |
| Each opted-in repo's `.git/hooks/{post-checkout,post-merge,reference-transaction}` | Symlinks into the installed git-hooks scripts above (step 14) |

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
  `workspace_prefixes` is set on the `PROJECT_CONFIGS` entry (step 11a) — this is not
  automatic and does not fail loudly if skipped
- **GitHub issues/PRs totals**: scoped per project via `PROJECT_CONFIGS[project]["issues_repos"]`
  in both `engram.pipeline.nightly_learn` and `engram.maintenance.report` — a project with no `issues_repos` key
  (e.g. the no-issues-bank variant) simply contributes nothing to any total, rather
  than defaulting to one hardcoded repo (the pre-2026-07-15 behavior; see FINDINGS.md)
- **Shared**: `cursor-memory` bank (behavioral corrections), Hindsight API instance, PostgreSQL
