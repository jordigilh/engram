# Issue Ingestion

Engram issue ingestion is performed by a project's CocoIndex flow. It is a
poller, not a filesystem watcher:

- GitHub Issues and pull requests are fetched with the authenticated `gh`
  CLI.
- Jira work items are fetched with Jira's REST API and retained in the
  project's `<project>-issues` bank.
- The default poll interval is 300 seconds.
- Local repository mirrors are not needed for issue ingestion; mirrors are
  used by the docs and code apps.

## GitHub

Authenticate `gh` as the operating-system user that runs the flow:

```bash
gh auth login
```

The account must be able to read every configured repository, including
private repositories. Configure the repository list and interval in the
project's `~/.engram/projects.toml` table:

```bash
[projects.<project>]
issues_repos = ["owner/repo", "owner/another-repo"]
issues_poll_seconds = 300
```

For the shipped flows, the relevant settings are:

| Flow | GitHub setting | Notes |
|------|----------------|-------|
| `kubernaut` | `projects.kubernaut.issues_repos` | Issues and pull requests |
| `praxis` | `projects.praxis.issues_repos` | Issues and pull requests; Jira keys are also supported |
| `kuadrant` | `projects.kuadrant.issues_repos` | Issues and pull requests |
| `dcm` | `projects.dcm.issues_repos` | Issues and pull requests |
| `koku` | `projects.koku.pr_repos` | Pull requests only; its work items come from Jira |

An unset project table leaves that deployment source unconfigured. For a new
flow, add a project table rather than editing source after deployment.

## Jira

The Python flows call Jira Cloud directly at
`/rest/api/3/search/jql`. They do not invoke the `jira` CLI as a subprocess.
The API token is read at runtime from the macOS Keychain item:

```text
account: jira-cli
service: jira-cloud-api-token
```

Verify the item without printing its value:

```bash
security find-generic-password \
  -a jira-cli -s jira-cloud-api-token -w >/dev/null
```

If it does not exist, add it with the password prompt as the final option:

```bash
security add-generic-password \
  -a jira-cli -s jira-cloud-api-token -w
```

Never put the token in `config.env`, a repository, or the Jira key file.

### Tracked-key scope

Flows that ingest a selected set of Jira work items read one key per line from
a local file. For example, the Praxis flow defaults to:

```text
~/.engram/scripts/jira-keys.txt
```

Override it with `<PROJECT>_JIRA_KEYS_FILE`, or provide a comma-separated list
with `<PROJECT>_JIRA_KEYS`. Blank lines and lines beginning with `#` are
ignored. Keep this file local and set its permissions to `0600`.

### Jira scope settings

Use the settings supported by the selected flow:

| Flow | Jira scope | Settings |
|------|------------|----------|
| `praxis` | Explicit tracked keys | `projects.praxis.paths.jira_keys_file`, `projects.praxis.jira_keys`, `projects.praxis.jira_server`, `projects.praxis.jira_email` |
| `koku` | Jira project, capped to recent work | `projects.koku.jira_project`, `projects.koku.jira_server`, `projects.koku.jira_email`, `projects.koku.jira_limit` |
| `rhdh-plugins` | Jira epic and its children | `projects.rhdh-plugins.jira_epic`, `projects.rhdh-plugins.jira_server`, `projects.rhdh-plugins.jira_email` |

Use a narrow JQL scope for a new Jira integration. Do not ingest an entire
large Jira project when only one epic or workstream is relevant.

## Start Ingestion

First confirm Hindsight is available and the target bank exists with the
project's configured LLM-free chunk settings:

```bash
curl -fsS http://localhost:8888/health
curl -fsS http://localhost:8888/v1/default/banks/<project>-issues
```

Run an immediate one-shot issue ingestion before installing a continuous
service. `with-config-env.sh` loads `~/.engram/config.env` for the process:

```bash
~/.engram/with-config-env.sh \
  ~/.engram/venv/bin/engram-flows-<project> \
  --mode backfill --apps issues
```

For continuous polling, render the project's tracked LaunchAgent template and
bootstrap it in the logged-in user's GUI domain:

```bash
sed "s|__HOME__|$HOME|g" \
  launchd/io.vectorize.cocoindex.<project>.plist \
  > "$HOME/Library/LaunchAgents/io.vectorize.cocoindex.<project>.plist"

launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.vectorize.cocoindex.<project>.plist"
launchctl kickstart -k "gui/$(id -u)/io.vectorize.cocoindex.<project>"
```

If the service is already loaded, run this idempotent reload instead:

```bash
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.vectorize.cocoindex.<project>.plist" \
  2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.vectorize.cocoindex.<project>.plist"
launchctl kickstart -k "gui/$(id -u)/io.vectorize.cocoindex.<project>"
```

The plist should run the installed `engram-flows-<project>` console script via
`~/.engram/with-config-env.sh` may still provide service credentials, but the
project's Hindsight/Postgres values and paths come from `projects.toml`; pass
`--apps issues` or the full app set. Deployment-specific paths and source
mirror configuration belong under `~/.engram`, not in this repository.

## Verify

Check the service and its project-specific stderr log:

```bash
launchctl print "gui/$(id -u)/io.vectorize.cocoindex.<project>"
grep -E 'Fetched .* issues|Fetched .* Jira issues|issues poll: complete' \
  "$HOME/.engram/logs/cocoindex-<project>-stderr.log" | tail -20
```

Expected evidence includes GitHub fetch counts, a Jira fetch count when Jira
is configured, and `issues poll: complete`. A missing Jira key file is a
configuration warning; a missing Keychain item is an authentication error.
Use the project's issues MCP tool to verify recall after ingestion, for
example by searching for a known issue title or Jira key.

## Troubleshooting

- `gh ... list failed`: run `gh auth status` and verify repository visibility.
- `Could not read ... Jira key file`: check the configured file path and
  permissions.
- `Could not read jira-cli API token`: recreate or authorize the Keychain item
  using the exact account/service pair above.
- `issues poll: complete` is absent: inspect the full project stderr log for
  Hindsight, network, or JSON errors; the poll loop logs errors and retries on
  the next interval.
- A fresh code/docs deployment should use `--mode backfill` before relying on
  live file watching. Issue polling itself can be run directly because it is
  API-backed.
