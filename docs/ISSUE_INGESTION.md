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

```toml
[projects.<project>]
issues_bank = "<project>-issues"
issues_repos = ["owner/repo", "owner/another-repo"]
issues_poll_seconds = 300
```

The configuration-driven adapter reads these settings for every project:

| Source | TOML setting | Notes |
|--------|--------------|-------|
| GitHub issues and pull requests | `projects.<project>.issues_repos` | Issues and pull requests |
| GitHub pull requests only | `projects.<project>.pr_repos` | Useful when work items come from another tracker |
| Jira work items | `projects.<project>.jira_project` or `projects.<project>.jira_jql` | Optional tracker-specific scope |

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

Use the settings supported by the configured project:

| Jira scope | Settings |
|------------|----------|
| Explicit tracked keys | `projects.<project>.paths.jira_keys_file`, `projects.<project>.jira_keys` |
| Project or JQL scope | `projects.<project>.jira_project`, `projects.<project>.jira_jql` |

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
  ~/.engram/venv/bin/engram-flows-configured --project <project> \
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

The generic plist runs `engram-flows-configured --project <project>` and reads
the project's Hindsight/Postgres values and issue repositories from
`~/.engram/projects.toml`. Deployment-specific paths and source mirror
configuration belong under `~/.engram`, not in this repository.

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
