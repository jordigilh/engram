# Issue parity fixtures

Sanitized payloads captured from live GitHub/Jira APIs during the #243 preflight:

- `github_pr.json`: a merged GitHub PR (`praxis-proxy/ai#2211`) with a bot
  comment. The bot login is normalized from REST's `praxis-bot-app[bot]` to
  `gh`'s `praxis-bot-app`, which is what preserves the existing
  `ai-pr-2211-comment0` Hindsight document.
- `jira_story.json`: a closed Jira story (`RHIDP-18446`) with an ADF
  description, a parent key, and one embedded comment.

The expected document ids, metadata, and tags mirror the live Hindsight
records (`ai-pr-2211`, `ai-pr-2211-comment0` and
`jira-RHIDP-18446`, `jira-RHIDP-18446-comment0`). Hindsight returns tags in a
canonical order, so tests compare the tag set rather than its order.
