# Engram

**Persistent memory traces for AI coding assistants.**

Engram gives your Cursor IDE agent memory that survives across sessions. Every
correction you make is encoded as a persistent trace — stored in a knowledge
graph, synthesized into mental models, and automatically surfaced in future
sessions so the same mistake never happens twice.

## How it works

```mermaid
flowchart LR
    subgraph session["During Sessions (zero LLM cost)"]
        A[Cursor Agent] -->|recall| B[Engram / Hindsight]
        B -->|"mental models + facts"| A
    end

    subgraph nightly["Nightly Batch"]
        C[Transcripts] -->|scan| D[Detect corrections]
        D -->|retain| E["Haiku 4.5 (extract)"]
        E -->|reflect| F["Sonnet 4.6 (synthesize)"]
    end
```

**Recall is local and free** — embeddings and reranking run on-device (~600ms).
LLM calls only happen overnight for pattern extraction.

## What it solves

| Problem | How Engram fixes it |
|---------|-------------------|
| Every session starts with amnesia | Recall surfaces past corrections automatically |
| Repeating the same mistakes | Corrections are stored as persistent patterns |
| Scattered knowledge across docs/issues | Mental models synthesize coherent context |
| No way to know if memory helps | Nightly metrics track correction reduction rate |

## Key features

- **Zero-cost recall** — local vector search, no tokens consumed during work
- **Learns from corrections** — detects when you correct the agent, extracts the lesson
- **Knowledge graph** — entities link across sessions for richer retrieval
- **Mental models** — pre-synthesized documents (not scattered facts)
- **Multi-bank architecture** — behavioral memory + project docs + GitHub issues
- **Self-evaluating** — proactive recall rate, correction reduction %, hit rates
- **Runs as macOS service** — launchd-managed, survives reboots, auto-restarts

## Quick start

```bash
git clone https://github.com/jordigilh/engram.git
cd engram
```

Then follow the [Installation Guide](docs/INSTALL.md) (takes ~15 minutes).

## Architecture

```mermaid
graph TB
    subgraph cursor["Cursor IDE"]
        rule["Rule (.mdc)"]
        hook["MCP Hook"]
        gopls["gopls MCP"]
    end

    subgraph engram["Engram (native macOS)"]
        api["Hindsight API :8888"]
        pg["Embedded Postgres"]
        emb["Local Embeddings"]
        rerank["Local Reranker"]
    end

    subgraph vertex["Vertex AI"]
        haiku["Haiku 4.5 (retain)"]
        sonnet["Sonnet 4.6 (reflect)"]
    end

    subgraph launchd["launchd"]
        svc["service (KeepAlive)"]
        nightly["nightly-learn (2 AM)"]
        issues["ingest-issues (1 AM)"]
    end

    cursor -->|"MCP ×3 banks"| api
    api --> pg
    api --> emb
    api --> rerank
    api -->|"retain / reflect"| vertex
    launchd --> api
    nightly --> api
    issues --> api
```

## Cost

| Operation | Model | Frequency | Cost |
|-----------|-------|-----------|------|
| Recall | Local (no LLM) | Every response | $0 |
| Retain | Haiku 4.5 | ~23 windows/night | ~$0.02 |
| Reflect | Sonnet 4.6 | Once/night | ~$0.10 |

**≈ $0.12/night** for a full learning cycle.

## Token savings

Each correction you make triggers a back-and-forth that costs tokens:

| Event | Tokens consumed | Why |
|-------|----------------|-----|
| Your correction message | ~200 | Explaining what went wrong |
| Agent re-reads context | ~2,000–8,000 | Reprocesses files to redo the work |
| Agent generates new response | ~1,000–4,000 | Redoes what it got wrong |
| **Total per correction** | **~3,000–12,000** | Wasted work that memory prevents |

With ~3 corrections/session across 5 sessions/day, that's **45,000–180,000
tokens/day** spent on repeated mistakes.

**What Engram saves:**

| Metric | Without Engram | With Engram | Savings |
|--------|---------------|-------------|---------|
| Corrections/session | ~3.2 | ~0.8 | 75% fewer corrections |
| Wasted tokens/day | ~120K | ~30K | ~90K tokens/day |
| Monthly waste (20 days) | ~2.4M tokens | ~600K tokens | **~1.8M tokens/month** |
| Engram operating cost | — | ~$3.60/month | — |

At typical Sonnet pricing (~$15/M output tokens), preventing 1.8M tokens of
wasted rework saves **~$27/month** for a cost of **$3.60/month** — a **7.5× ROI**.

> These estimates are based on early data (correction reduction of ~75% in
> sessions where recall is active). Your actual savings depend on how frequently
> the agent makes correctable mistakes in your workflow. Run `python3 report.py`
> to see your measured reduction rate.

## Documentation

| Doc | Content |
|-----|---------|
| [Installation Guide](docs/INSTALL.md) | Full setup, prerequisites, verification |
| [Customizing the Rule](docs/INSTALL.md#customizing-the-rule) | Adapt for your project (Python, Rust, etc.) |
| [Architecture & Internals](docs/README.md) | Design decisions, knowledge graph, correction detection |
| [Metrics & Monitoring](docs/METRICS.md) | Effectiveness tracking, proactive recall, report interpretation |

## License

MIT
