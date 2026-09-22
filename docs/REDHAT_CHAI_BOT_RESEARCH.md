# Red Hat Chai Bot and Fullsend Research

**Recorded**: 2026-09-22

This document records the public findings about `redhat-chai-bot`, Chai Bot,
and the related Fullsend agent harness. It deliberately separates verified
facts from inferences about private systems.

## Verified Chai Facts

- Fullsend issue [#5387](https://github.com/fullsend-ai/fullsend/issues/5387)
  identifies `redhat-chai-bot` as the service account used by Chai Bot, an AI
  assistant for Red Hat engineering teams.
- The same issue states that Chai opens pull requests on behalf of team
  members and that each pull request is explicitly requested and approved by a
  Red Hat engineer. It explicitly says the account does not act autonomously.
- The account has a public fork of Fullsend:
  [`redhat-chai-bot/fullsend-ai_fullsend`](https://github.com/redhat-chai-bot/fullsend-ai_fullsend).
- It submitted Fullsend [PR #5386](https://github.com/fullsend-ai/fullsend/pull/5386),
  a documentation change requested by a Red Hat engineer.
- The public source repository referenced for Chai,
  [`openshift-eng/ship-help-bot`](https://github.com/openshift-eng/ship-help-bot),
  was inaccessible during research. Chai-specific implementation details
  therefore remain unverified.

## Fullsend Memory Findings

The public [Fullsend repository](https://github.com/fullsend-ai/fullsend) and
its [cross-run memory design document](https://fullsend.sh/docs/problems/cross-run-memory)
show no dedicated vector, episodic, or conversation-memory store.

- Runs are stateless by default.
- OpenShell sandboxes are ephemeral and destroyed after extraction.
- Durable knowledge is represented through reviewed `AGENTS.md`, `CLAUDE.md`,
  skills, harness configuration, and GitHub artifacts.
- The retro agent analyzes completed workflows and proposes improvements as
  GitHub issues rather than directly writing trusted memory.
- Transcripts, telemetry, validation feedback, metrics, and remote-resource
  fetch caches persist as run artifacts, but `fullsend run` does not
  automatically inject prior run artifacts into later prompts.
- Fullsend treats automatic cross-run memory as a prompt-injection and
  governance risk. The design calls for provenance, scoping, expiration, and
  review before durable instructions influence later agents.

## Non-LLM Harness

Fullsend's deterministic runner performs the following lifecycle:

1. Compose a harness YAML definition.
2. Run host-side pre-scripts.
3. Scan repository context for injection, Unicode, SSRF, and secrets.
4. Provision one isolated OpenShell sandbox per agent.
5. Inject agent definitions, skills, configuration, and security hooks.
6. Run Claude, Pi, Codex, or test-only dummy runtimes.
7. Extract outputs, transcripts, telemetry, and metrics.
8. Validate structured output with bounded retries and validation feedback.
9. Run host-side post-scripts.
10. Delete the sandbox.

Multi-agent sequencing is handled by CI workflows. The runner itself is
intended to execute one agent in one sandbox.

## Context Sources

The agent receives context from:

- Repository code and files.
- `AGENTS.md` and `CLAUDE.md`.
- Explicit skills and harness configuration.
- Issue, pull request, review, and event data supplied by pre-scripts.
- Organization architecture and policy documentation.
- Current-run transcripts, metrics, and validation output.

There is no public evidence that Fullsend maintains automatic personal or
cross-project model memory.

## Confidence and Limits

- **High confidence**: the public Fullsend architecture, its stateless-run
  memory position, and the explicit description of the Chai service account in
  Fullsend issue #5387.
- **Lower confidence**: any claim about Chai's private memory or execution
  implementation, because the referenced `ship-help-bot` source was not
  publicly accessible.
- Fullsend is a public system that Chai interacts with. The public evidence
  does not establish that Fullsend is Chai's internal runtime or memory
  implementation.
