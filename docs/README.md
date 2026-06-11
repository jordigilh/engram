# Hindsight: Agent Memory for Cursor IDE

## Overview

Hindsight is an agent memory system that enables Cursor to **learn from past mistakes** and **recall relevant patterns** across sessions. Instead of every conversation starting from zero, the AI assistant recalls what worked, what didn't, and what you've corrected before.

### The Problem

Every Cursor session starts with amnesia. The assistant makes the same mistakes repeatedly:
- Writes implementation before tests (TDD violations)
- Uses wrong naming conventions (snake_case vs camelCase)
- Targets wrong build architectures
- Assumes credentials flow instead of reading the code

You correct it. Next session, it forgets. You correct it again.

### The Solution

Hindsight provides a **memory layer** that sits between Cursor and your LLM provider:

```
┌─────────────────────────────────────────────────────────┐
│                    During Sessions                        │
│                                                          │
│   Cursor Agent ──recall──▶ Hindsight (local, ~600ms)    │
│       │                        │                         │
│       │                   Local embeddings               │
│       │                   Local reranker                 │
│       ▼                   No LLM call needed             │
│   Response informed by past corrections                  │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                   Nightly (midnight)                      │
│                                                          │
│   Transcripts ──scan──▶ Detect corrections               │
│       │                                                  │
│       ▼                                                  │
│   Correction windows ──retain──▶ Haiku 4.5 (extract)    │
│       │                                                  │
│       ▼                                                  │
│   Patterns ──reflect──▶ Sonnet 4.6 (synthesize)         │
└─────────────────────────────────────────────────────────┘
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Recall-only during sessions** | Zero token cost, pure local vector search (~600ms) |
| **Retain in nightly batch** | Avoids hitting token quotas during work hours |
| **Haiku 4.5 for extraction** | 10x cheaper than Sonnet for structured pattern extraction |
| **Sonnet 4.6 for reflection** | Complex reasoning about what patterns are effective |
| **Correction-focused learning** | Only learns from moments you corrected the assistant |
| **Global endpoint** | Single Vertex AI endpoint, no region-specific routing |
| **Local embeddings + reranker** | No network calls for recall; runs on-device |

### What It Improves

1. **Reduces repeated mistakes** — corrections are remembered across sessions
2. **Learns coding conventions** — naming, architecture, workflow preferences
3. **Zero-cost recall** — no LLM tokens consumed during active work
4. **Automatic** — no manual tagging or bookmarking needed
5. **Self-evaluating** — nightly reflect identifies which patterns are most impactful
6. **Knowledge RAG** — project documentation recalled alongside behavioral memory
7. **Go code intelligence** — type-aware navigation via gopls MCP (no source ingestion)

### Cost Profile

| Operation | Model | Tokens/call | Frequency |
|-----------|-------|-------------|-----------|
| Recall | Local (no LLM) | 0 | Every response |
| Retain | Haiku 4.5 | ~4,500 | ~23 windows/night |
| Reflect | Sonnet 4.6 | ~64,000 | Once/night |

**Estimated nightly cost**: ~100K Haiku tokens + ~64K Sonnet tokens ≈ **$0.12/night**

---

## Architecture

```
┌──────────────┐     MCP (HTTP)      ┌──────────────────────┐
│  Cursor IDE  │◀───────────────────▶│  Hindsight Container │
│              │   recall_memory()    │                      │
│  mcp.json   │                      │  - FastAPI server    │
│  rule .mdc  │                      │  - Embedded Postgres │
└──────────────┘                      │  - ONNX embeddings  │
                                      │  - ONNX reranker    │
                                      │  - LiteLLM → Vertex │
                                      └──────────┬───────────┘
                                                  │
                                                  │ retain / reflect
                                                  ▼
                                      ┌──────────────────────┐
                                      │  Vertex AI (global)  │
                                      │                      │
                                      │  - Haiku 4.5 (retain)│
                                      │  - Sonnet 4.6(reflect│
                                      └──────────────────────┘

┌──────────────┐     Nightly job      ┌──────────────────────┐
│   launchd    │────────────────────▶│  nightly-learn.py    │
│  (midnight)  │                      │                      │
└──────────────┘                      │  1. Scan transcripts │
                                      │  2. Detect corrections│
                                      │  3. Retain patterns  │
                                      │  4. Reflect          │
                                      │  5. Log results      │
                                      └──────────────────────┘
```

### Components

| Component | Location | Purpose |
|-----------|----------|---------|
| Project source | `~/go/src/github.com/jordigilh/recollect/` | Code pushed to GitHub |
| LLM config | `~/.hindsight/config.env` | Real project IDs, model names (never committed) |
| Hindsight container | `localhost:8888` | Memory API + storage |
| Control Plane UI | `localhost:9999` | Web dashboard for browsing memories |
| MCP config | `~/.cursor/mcp.json` | Connects Cursor to Hindsight + docs bank + gopls |
| Cursor rule | `~/.cursor/rules/hindsight-memory.mdc` | Instructs agent to recall from both banks |
| Nightly script | `nightly-learn.py` (symlinked to `~/.hindsight/`) | Processes transcripts, extracts patterns |
| Ingestion script | `ingest-docs.py` | One-time doc ingestion into knowledge bank |
| launchd plist | `~/Library/LaunchAgents/io.vectorize.hindsight.nightly.plist` | Schedules midnight execution |
| Persistent storage | `~/.hindsight/data/` | PostgreSQL data (survives container restarts) |
| Logs | `~/.hindsight/logs/` | Daily JSON reports + recall-signals.jsonl |

### Memory Banks

| Bank | Content | Extraction Mode | LLM Cost |
|------|---------|-----------------|----------|
| `cursor-memory` | Corrections, instructions, workflow patterns | `concise` | Haiku 4.5 per window |
| `kubernaut-docs` | Published architecture, API, operations docs | `chunks` | $0 (embeddings only) |

### Security Boundary

```
GitHub (public)                    Local only (~/.hindsight/)
─────────────────                  ────────────────────────────
Dockerfile                         config.env (project IDs, model config)
start.sh (reads config.env)        data/ (PostgreSQL)
nightly-learn.py                   logs/ (daily reports)
docs/                              application_default_credentials.json
config.env.example (placeholders)
.githooks/pre-commit (blocks leaks)
```

---

## How Correction Detection Works

The nightly script scans Cursor agent transcripts (`.jsonl` files) for user messages that indicate the assistant made a mistake. It uses targeted regex patterns:

```python
"no that's wrong/incorrect"      # explicit rejection
"don't do that"                  # behavioral correction
"I said/meant ..."               # clarification of prior intent
"wrong file/path/approach/..."   # specific error callout
"that broke"                     # caused a failure
"undo that/this"                 # revert request
"that's not what I..."           # expectation mismatch
"you shouldn't have..."          # retrospective correction
"do not use / we don't use"     # convention enforcement
```

For each correction, a **window** of surrounding context is extracted (2 messages before + correction + 2 messages after). Only these focused windows are sent to Hindsight — not the full transcript.

### Example

```
[Context] User: deploy the service to staging
[Context] Assistant: Built image for linux/arm64 and pushed to ghcr.io...
[CORRECTION] User: wrong architecture, we deploy amd64. And we use quay.io not ghcr.
[Context] Assistant: You're right, rebuilding for linux/amd64 and pushing to quay.io...
```

Hindsight extracts: *"Build architecture must be linux/amd64 for staging deployments. Container registry is quay.io, not ghcr.io."*

Next session, when the user asks to deploy, recall surfaces this pattern.
