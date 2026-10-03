# Installation Guide

> **On Linux/Fedora/RHEL?** This guide covers the macOS-native install
> (Hindsight runs as a bare process, no container). See
> [`INSTALL-linux.md`](INSTALL-linux.md) instead for the platform-specific
> steps (containerized Hindsight via Podman Quadlets and native Engram services
> via systemd) — the platform-agnostic sections below apply on both platforms,
> and that guide links back here for them.

## Prerequisites

- macOS (tested on Mac Studio M2 Max, 32GB RAM)
- Python 3.14 (`uv` manages this automatically, or `brew install python@3.14`)
- [uv](https://docs.astral.sh/uv/) — fast Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- [gh](https://cli.github.com/) — GitHub CLI for issues ingestion (`brew install gh && gh auth login`)
- [jq](https://jqlang.github.io/jq/) — JSON processor for MCP hook (`brew install jq`)
- `pip install cocoindex==1.0.23` (or `uv pip install cocoindex==1.0.23`) — incremental ingestion engine
- For code indexing: no separate install needed — `cocoindex` bundles its own tree-sitter-backed AST chunking (`cocoindex.ops.text.RecursiveSplitter`) and hybrid search (dense + BM25) out of the box.
- Google Cloud SDK (`gcloud`) with Application Default Credentials configured
- Vertex AI API enabled on your GCP project
- Claude models enabled on Vertex AI (Haiku 4.5 + Sonnet 4.6)
- OpenCode/OpenChamber (Cursor rules/hooks remain optional)

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
mkdir -p ~/.engram/logs
cp config.env.example ~/.engram/config.env
```

Edit `~/.engram/config.env` and fill in your GCP project ID:

```bash
$EDITOR ~/.engram/config.env
```

> **Important**: `~/.engram/config.env` contains runtime credentials, secrets,
> and LLM settings and stays local. Deployment paths, repositories, index policy,
> and RCA settings belong in `~/.engram/projects.toml`; neither populated file
> is committed to this repo.

## 4. Install Hindsight (native)

```bash
uv venv ~/.engram/hindsight-venv --python 3.13
uv pip install --python ~/.engram/hindsight-venv/bin/python \
  'hindsight-api==0.10.0'
```

This installs Hindsight with embedded PostgreSQL (pg0), local ONNX embeddings,
and local reranker in its own native Python environment. The separate venv is
intentional: Hindsight 0.10.0 requires a patched LiteLLM/protobuf combination
that cannot coexist with Engram's Vertex AI environment. Data persists at
`~/.pg0/instances/hindsight/data/`.

### Recommended for always-on/multi-project setups: decouple Postgres

By default `hindsight-api` starts, stops, and health-checks its own embedded
Postgres (`pg0`) as part of its own process lifecycle. This is fine for a
quick single-user trial, but on a machine running CocoIndex too (which shares
this same Postgres instance/database for its pgvector tables — see
`defaults.pg_dsn` in `~/.engram/projects.toml`) and running `hindsight-api` as an unattended
launchd service (step 5), it couples Postgres's uptime to two independent
failure modes that have nothing to do with Postgres itself: a `pg0` liveness
check that can false-positive on a stale/reused PID, and any future script
that restarts `hindsight-api` (e.g. the nightly heap-reclaim swap in step 5)
being unable to `launchctl bootstrap` without an active GUI session. Both of
these took Postgres down within the same week on the reference deployment —
see [docs/findings/2026-08.md](findings/2026-08.md).

To run Postgres as its own independently-supervised launchd job instead
(same binary, same data directory — no data migration):

```bash
# Stop the pg0-managed instance first (only if hindsight-api/pg0 already
# started it once, e.g. after step 4 or an earlier run of ./start.sh):
~/.pg0/installation/*/bin/pg_ctl -D ~/.pg0/instances/hindsight/data stop -m fast

sed "s|__HOME__|$HOME|g" launchd/io.vectorize.hindsight.postgres.plist \
    > ~/Library/LaunchAgents/io.vectorize.hindsight.postgres.plist

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.vectorize.hindsight.postgres.plist
```

Then add this to `~/.engram/config.env` so `hindsight-api` connects to it
directly instead of trying to manage `pg0` itself (see the comment above this
var in `config.env.example` for the full rationale):

```bash
HINDSIGHT_API_DATABASE_URL=postgresql://hindsight:hindsight@localhost:5432/hindsight
```

Restart `hindsight-api` (step 5) after adding this so it picks up the new
value. Omit this var entirely to keep using pg0-embedded's default
self-managed behavior.

## 5. Start the service

```bash
./start.sh
```

This sources `~/.engram/config.env` and runs the native `hindsight-api` binary.

> **Important**: Use `./start.sh` for development OR launchd for production.
> Do not run both simultaneously — they bind the same port (8888).

For production, install as a launchd service (auto-start on login, auto-restart
on crash). First install the shared env wrapper — **every** launchd plist that
runs a process needing LLM config (`hindsight-api`, `engram-nightly-learn`,
`engram-flows-*`, and `prefilter-shadow-trial.py`) launches through this wrapper
instead of having deployment-specific credentials or project IDs baked into
the plist:

```bash
cp with-config-env.sh ~/.engram/with-config-env.sh
chmod +x ~/.engram/with-config-env.sh
```

> **Why not hardcode it into the plist?** `~/.engram/config.env` and
> `~/.engram/projects.toml` are the only places these deployment values should
> live — plists under `launchd/` are committed to this (public) repo, so nothing
> project/LLM-specific gets baked in at generation time. `with-config-env.sh`
> sources `config.env` fresh into the process
> environment every time launchd starts the job, so it's read once, in one
> place, always current. See [FINDINGS.md](FINDINGS.md) 2026-07-27 for the
> production incident this replaced (launchd jobs silently running against a
> nonexistent placeholder GCP project because the value was missing from their
> environment entirely).

`hindsight-api` runs as a **blue/green pair** behind a small always-on proxy,
not as a single service — this is what lets the nightly heap-reclaim restart
(step 15 below) happen without ever dropping the gateway-backed MCP connection to port
8888 (see [FINDINGS.md](FINDINGS.md) 2026-08-02 for why). Install the proxy
and swap script, then install all three plists (proxy + both colors):

```bash
ln -sf "$(pwd)/src/engram/pipeline/hindsight_proxy.py" ~/.engram/hindsight-proxy.py
ln -sf "$(pwd)/hindsight-blue-green-restart.sh" ~/.engram/hindsight-blue-green-restart.sh
mkdir -p ~/.engram/state

for name in proxy service-blue service-green; do
  sed "s|__HOME__|$HOME|g" \
      "launchd/io.vectorize.hindsight.${name}.plist" \
       > "$HOME/Library/LaunchAgents/io.vectorize.hindsight.${name}.plist"
done

# "blue" (internal port 18888) is the initial active color.
echo 18888 > ~/.engram/state/active-backend.port

launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.service-blue.plist
launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.proxy.plist
```

> **Why not just one `service.plist`?** A single process bound directly to
> 8888 means the nightly restart has to unbind that port for the ~15-20s it
> takes to come back up — and an HTTP MCP client doesn't auto-retry
> after a drop, so every restart silently disabled the hindsight MCP tools
> until a manual reload. With the proxy owning 8888 permanently and
> `hindsight-blue-green-restart.sh` starting a fresh instance on the
> *standby* color, health-checking it, and only then flipping traffic over,
> port 8888 never refuses a connection — see
> [FINDINGS.md](FINDINGS.md) 2026-08-02.

> **Note on model names**: Sonnet 4.6 must be specified WITHOUT a version suffix on the
> global endpoint. Haiku 4.5 works with `@20251001`.

### Optional: nightly heap-reclaim restart

`hindsight-api`'s Python heap grows unbounded over a long-lived process
(see [FINDINGS.md](FINDINGS.md) 2026-06-26) — a periodic restart is the
practical fix. Install the swap job to run nightly at 1 AM:

```bash
sed "s|__HOME__|$HOME|g" launchd/io.vectorize.hindsight.restart.plist \
    > ~/Library/LaunchAgents/io.vectorize.hindsight.restart.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.hindsight.restart.plist
```

This runs `hindsight-blue-green-restart.sh` (installed in step 5 above), not
a raw `pkill` — see that step's note for why.

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

## 7. Configure OpenCode

Configure the OpenCode plugin and unified Engram gateway as described in
[`OPENCODE.md`](OPENCODE.md). Keep one project-scoped gateway route and let the
plugin own the backend connections.

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

## 9. Install the engram package

Everything under `src/engram/` (shared modules, per-project CocoIndex flows/
search, the learning pipeline, maintenance scripts) is a real, pip-
installable package now — one editable install replaces the old per-module
symlinking (`correction_gate.py`, `contradiction_resolution.py`,
`project_scope.py`, and a `spike/` path hack are no longer symlinked
individually; they're just importable as `engram.*` from anywhere once
installed):

```bash
uv venv ~/.engram/venv --python 3.14
uv pip install --python ~/.engram/venv/bin/python -e .
uv pip install --python ~/.engram/venv/bin/python 'google-cloud-aiplatform>=1.38'
```

This generates the console scripts used by the launchd templates:
`engram-flows-*`, `engram-search-*`, `engram-nightly-learn`, and the shared
service entry points. Manual tools such as reports and backfills can be run as
`python3 -m engram.<subpackage>.<module>`.

Project source paths and transcript prefixes are deployment settings. Add them
to `~/.engram/projects.toml`; do not edit `project_scope.py` or add path
environment variables for a new project.

## 10. Schedule always-on services with launchd

The Hindsight service and the project-specific CocoIndex flow are the
always-on services. LLM learning, reflection, and triage are intentionally
on-demand; do not enable the legacy hourly/nightly learning plists by default.
Use `engram-nightly-learn` manually when a deliberate maintenance run is
wanted.

## 11. Ingest project documentation (Knowledge RAG)

Documentation is owned by the project's CocoIndex flow. For a new project, use
the [backfill-before-live checklist](NEW_PROJECT_SETUP.md#install-backfill-and-start-the-flow)
or run the configured flow with `--mode backfill --apps docs`; do not use a
standalone importer for the normal deployment path.

## 12. Ingest issues (Knowledge RAG)

Issue ingestion is now owned by each project's CocoIndex flow. The complete
GitHub/Jira configuration, credential setup, immediate backfill, LaunchAgent
startup, and verification procedure is documented in
[`ISSUE_INGESTION.md`](ISSUE_INGESTION.md).

Do not use the retired standalone importer for a CocoIndex-managed project.

## 13. Create mental models (Knowledge Graph)

Mental models are LLM-synthesized documents that sit above raw facts in the recall
hierarchy. They provide pre-digested, coherent context blocks — reducing the need
for the agent to synthesize scattered individual facts at query time.

```bash
python3 -m engram.maintenance.create_mental_models
```

This creates and refreshes the models configured for the deployment (the
reference Kubernaut setup has 9 models; initial refresh costs about $0.50 with
Sonnet 4.6). To check status:

```bash
python3 -m engram.maintenance.create_mental_models --list
```

Behavioral, issue, and docs models refresh during an explicit maintenance run.
Refresh them manually after documentation or issue scope changes:

```bash
python3 -m engram.maintenance.create_mental_models --refresh
```

## 14. Configure code intelligence

Type-aware code intelligence is provided by the gateway's Serena backend through
the OpenCode plugin. Do not configure a separate `gopls` entry; see
[OpenCode Integration](OPENCODE.md).

## 15. Install the observability hook

This hook logs MCP calls through the Engram gateway for effectiveness monitoring:

```bash
mkdir -p ~/.cursor/hooks
cp cursor/hooks/log-mcp-calls.sh ~/.cursor/hooks/
chmod +x ~/.cursor/hooks/log-mcp-calls.sh
sed "s|__HOME__|$HOME|g" cursor/hooks.json > ~/.cursor/hooks.json
```

## 16. CocoIndex Setup (macOS launchd)

CocoIndex replaces legacy batch importers with continuous, incremental sync for
docs, issues, code, and transcripts.

### Install CocoIndex into the Engram venv

```bash
uv pip install --python ~/.engram/venv/bin/python cocoindex==1.0.23
```

`pdfplumber` (PDF text extraction) rides along as a transitive dependency of
`cocoindex` at this point, but any flow that ingests manually-curated PDFs
(e.g. `engram.flows.praxis`'s `process_pdf_file`, for supplementary
project-overview PDFs dropped in `~/.engram/manual-docs/<project>/`) does
`import pdfplumber` directly, so pin it explicitly rather than relying on an
undeclared transitive dependency:

```bash
uv pip install --python ~/.engram/venv/bin/python pdfplumber
```

### Configure and backfill

The launchd templates invoke the installed `engram-flows-*` and
`engram-search-*` console scripts directly; no flow or search symlinks are
needed. Configure source paths in `~/.engram/projects.toml`, run a backfill,
then install the matching continuous-sync plist. For a new project, use
[`NEW_PROJECT_SETUP.md`](NEW_PROJECT_SETUP.md).

### Configure source directories

Copy `docs/projects.toml.example` to `~/.engram/projects.toml` and configure the
`[projects.kubernaut.paths]` table:

```toml
[projects.kubernaut]
issues_repos = [
  "jordigilh/kubernaut",
  "jordigilh/kubernaut-operator",
  "jordigilh/kubernaut-console",
  "jordigilh/kubernaut-demo-scenarios",
  "jordigilh/kubernaut-docs",
]
issues_poll_seconds = 300

[projects.kubernaut.paths]
docs_dir = "~/.engram/watch/kubernaut-docs/docs"
code_dir = "~/go/src/github.com/jordigilh/kubernaut"
```

Keep `~/.engram/config.env` for service credentials and Hindsight runtime
settings; repository paths and project routing belong in `projects.toml`.

### Run initial backfill

```bash
~/.engram/venv/bin/engram-flows-kubernaut --mode backfill
```

This processes all existing docs, issues, code, and transcripts. Subsequent runs
use delta processing (only changed content is re-ingested).

### Install launchd plist (continuous sync)

On Linux, use the `engram-cocoindex.service` systemd unit from
[`INSTALL-linux.md`](INSTALL-linux.md#8-schedule-the-always-on-ingestion-service)
instead of installing this launchd plist.

```bash
sed "s|__HOME__|$HOME|g" launchd/io.vectorize.cocoindex.service.plist \
  > ~/Library/LaunchAgents/io.vectorize.cocoindex.service.plist

launchctl load ~/Library/LaunchAgents/io.vectorize.cocoindex.service.plist
```

### Verify

```bash
# Check all four flows started
grep "Starting\|Fetched\|poll:" ~/.engram/logs/cocoindex-stderr.log | tail -10

# Check issues + PRs are fully indexed
grep "Fetched.*from" ~/.engram/logs/cocoindex-stderr.log | tail -5
```

You should see all four apps starting (docs, code, transcripts, issues) and
issue poll cycles completing with the full count of issues + PRs. See
[CocoIndex Operations](COCOINDEX.md) for monitoring and troubleshooting details.

> **Call-graph tools**: every onboarded project's search MCP server also
> exposes `--blast-radius`/`--shortest-path`/`--cluster` (structural
> call-graph queries) alongside `--query`/`--pattern` above -- no extra setup
> needed, they reuse the same live checkout. kubernaut specifically caches
> its (larger, slower-to-build) graph in Postgres rather than rebuilding on
> every call, using the `defaults.pg_dsn` connection already configured
> above -- no new service to install. See
> [CocoIndex Operations](COCOINDEX.md#graphify-inspired-call-graph-queries) for why Postgres
> was chosen over a dedicated cache service, and for kubernaut's
> `main`/`release-vX.Y` branch-scoping behavior.

> **Onboarding additional projects**: each project gets its own flow/search
> modules, console-script entries, deployment table, and flow plist. There is
> no per-project flow or search symlink. See
> [NEW_PROJECT_SETUP.md](NEW_PROJECT_SETUP.md) for the checklist, including
> the tag-scoped variant for sub-repos that do not need a separate pipeline.

## 17. Reload the client

Reload the OpenCode/OpenChamber host after changing the gateway route, plugin,
rule, or hook configuration.

## Verification

After restarting the OpenCode/OpenChamber host, open a new chat. The agent
should now call `recall_memory` before responding.

To manually test a maintenance run:

```bash
~/.engram/venv/bin/engram-nightly-learn
```

Check results:

```bash
cat ~/.engram/logs/$(date +%Y-%m-%d).json | python3 -m json.tool
```

Generate an effectiveness report:

```bash
python3 -m engram.maintenance.report          # last 7 days
python3 -m engram.maintenance.report --days 30  # last 30 days
python3 -m engram.maintenance.report --json     # machine-readable
```

See [METRICS.md](METRICS.md) for full details on what's tracked and how to
interpret the results.

---

## Running the Test Suite

The `tests/` directory has a `pytest` regression suite covering the shared
modules (`engram.correction_gate`, `engram.contradiction_resolution`,
`engram.project_scope`), the `engram.hindsight_client` recall client,
`engram.maintenance.review_contradictions`'s approve/reject/skip/quit flow,
and the core retain logic in `engram.pipeline.nightly_learn` and
`engram.flows.kubernaut`. Added 2026-07-13 after three real bugs shipped to
production in one session with zero automated coverage catching any of them
(see [FINDINGS.md](FINDINGS.md)).

Install the `dev` extra into the same venv the production scripts run under
(this is the same `pip install -e .` from step 9, plus `pytest`/`ruff`):

```bash
uv pip install --python ~/.engram/venv/bin/python -e ".[dev]"
```

Run the suite:

```bash
~/.engram/venv/bin/python3 -m pytest tests/ -m "not integration" -v
```

The suite is fully offline — every LLM call (Haiku classification, Sonnet
contradiction check), Hindsight API call, and CocoIndex file-watch is mocked
via `pytest`'s `monkeypatch` fixture, so it runs in well under a second and
never touches your live `~/.engram/` data or costs any tokens. `conftest.py`
provides fixtures (`nightly_learn`, `cocoindex_flows`, `review_contradictions`,
`purge_script`, etc.) that import the corresponding `engram.*` package modules
directly — no `sys.path` hacks needed since `engram` is a real installed
package. CI (`.github/workflows/ci.yml`) runs this same suite plus
`ruff check .` on every push/PR.

### Integration tests (`tests/integration/`)

A second, opt-in tier under `tests/integration/` runs against a real
Postgres+pgvector container (via Podman) rather than mocks — see
[`tests/integration/README.md`](../tests/integration/README.md) for setup
and the `-m integration` invocation. Excluded from the command above by
`-m "not integration"`; CI runs it as a separate `integration-tests` job
against a service container. Never point it at the shared dev Postgres on
`localhost:5432`.

---

## Troubleshooting

### Service won't start
```bash
launchctl list | grep hindsight
tail -50 ~/.engram/logs/hindsight-stderr.log
```

### Recall returns empty results
The memory bank needs at least one retained item. Run `engram-nightly-learn`
manually or retain a test memory.

### Retain fails with "Could not resolve project_id"
Ensure `defaults.gcp_project` is set in `~/.engram/projects.toml`, or set
`VERTEXAI_PROJECT` and `GOOGLE_CLOUD_PROJECT` in `~/.engram/config.env`.

### Reflect returns 404
Sonnet 4.6 on the global endpoint requires the model name WITHOUT a version suffix. Use `vertex_ai/claude-sonnet-4-6`, not `vertex_ai/claude-sonnet-4-6@20250929`.

### Which color is currently active?
```bash
cat ~/.engram/state/active-backend.port   # 18888 = blue, 18889 = green
launchctl list | grep hindsight.service-      # the loaded one is active
```

### ADC token expired
```bash
gcloud auth application-default login
# Restart whichever color is currently active (see above), e.g.:
launchctl kickstart -k gui/$(id -u)/io.vectorize.hindsight.service-blue
```

### Manually force a blue/green swap
```bash
~/.engram/hindsight-blue-green-restart.sh
tail -20 ~/.engram/logs/blue-green-restart.log
```

---

## Upgrading

```bash
uv pip install --python ~/.engram/hindsight-venv/bin/python 'hindsight-api==0.10.0'
~/.engram/hindsight-blue-green-restart.sh
```

The blue/green swap above starts the new package version on the standby
color, health-checks it, then cuts over — so the upgrade itself never drops
the gateway-backed client connection either.

Verify after upgrade:

```bash
curl -s http://localhost:8888/health | python3 -m json.tool
```

---

## Customizing the Rule

The included `hindsight-memory.mdc` rule is tailored for kubernaut (a Go operator
project with CocoIndex search and gateway-provided Serena). Adapt it for your own project
by copying one of the ready-made examples below and tweaking the domain triggers.

### Ready-made examples

Example rules live in `cursor/examples/`. Each is a complete, copy-ready `.mdc`
file with the planning gate, mid-session re-recall, and phase-based triggers
already wired in:

| Example | Stack | File |
|---------|-------|------|
| Go operator | Go, K8s, CRDs, Serena, CocoIndex | [`cursor/examples/go-operator.mdc`](../cursor/examples/go-operator.mdc) |
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
   relying on Grep/SemanticSearch. Requires the CocoIndex setup above.
5. **Phase-based triggers** — recall the right bank or call `cocoindex_search`
   when transitioning to a new phase (planning, testing, API design, debugging,
   code exploration, refactoring)
6. **Skip criteria** — prevents recall spam on trivial follow-ups
7. **Do NOT retain** — blocks in-session retain calls (extraction is an explicit maintenance action)

### Adapting an example

When customizing, change:

1. **Domain triggers** — replace language/framework mentions with your stack
2. **Banks** — adjust the project banks configured in `projects.toml`
   (`<project>-docs` and `<project>-issues` when those sources are enabled)
3. **Phase-based queries** — tailor query focus to your project's terminology
   (e.g., "pytest fixtures" vs "table-driven tests")
4. **Language tooling** — select the project language for the gateway's Serena
   backend; do not add a separate client MCP entry

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
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.service-blue.plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.service-green.plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.proxy.plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.hindsight.restart.plist
launchctl unload ~/Library/LaunchAgents/io.vectorize.cocoindex.service.plist
rm ~/Library/LaunchAgents/io.vectorize.hindsight.*.plist
rm ~/Library/LaunchAgents/io.vectorize.cocoindex.*.plist

# Remove data and runtime
rm -rf ~/.engram ~/.pg0

# Remove optional Cursor rule/hook integration
rm ~/.cursor/rules/hindsight-memory.mdc
rm ~/.cursor/hooks.json
rm -rf ~/.cursor/hooks/log-mcp-calls.sh

```
