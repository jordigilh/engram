# Metrics and Effectiveness Monitoring

## Overview

Engram tracks three categories of metrics to evaluate whether the memory system
reduces mistakes and improves productivity:

1. **MCP Usage** — How often each tool is called and whether it returns useful results
2. **Effectiveness** — Correlation between recall usage and correction rates
3. **Recall Quality** — Latency and result counts from nightly health probes

## Data Collection

### Real-time: Cursor Hook

A `afterMCPExecution` hook logs every MCP tool call as it happens:

```
~/.hindsight/logs/mcp-calls.jsonl
```

Each line contains:
```json
{
  "ts": "2026-06-11T15:30:00",
  "server": "hindsight",
  "tool": "recall",
  "hit": true,
  "result_chars": 1200,
  "duration_ms": 850,
  "is_error": false
}
```

**Hit/miss classification:**
- `hit = true`: result contained >10 characters of content
- `hit = false`: empty result, error, or no useful content returned

### Nightly: Effectiveness Analysis

The nightly script (`nightly-learn.py`) produces two outputs:

**Daily report** (`~/.hindsight/logs/YYYY-MM-DD.json`):
- Corrections detected per transcript
- Instructions detected
- Token usage for retain/reflect
- Bank stats (nodes, documents, links)
- Recall probe latency and results

**Effectiveness report** (`~/.hindsight/logs/effectiveness-report.jsonl`):
- Per-server call counts and hit rates
- Correction rate comparison (sessions with recall vs. without)
- Estimated correction reduction percentage
- Session length proxy (message count)

## Metrics Definitions

| Metric | Formula | What it measures |
|--------|---------|-----------------|
| **Hit Rate** | hits / total_calls | Retrieval quality — are queries returning relevant content? |
| **Correction Rate** | corrections / sessions | How often you need to correct the assistant |
| **Reduction %** | 1 - (rate_with / rate_without) | Improvement from memory: fewer corrections when recall is active |
| **Avg Session Length** | messages / sessions | Proxy for token cost — shorter sessions = fewer tokens |
| **Recall Latency** | ms per recall call | Performance health — should be <2s for good UX |
| **Result Count** | chunks returned per recall | Coverage — more results = richer context |

## Generating Reports

### Quick summary (last 7 days)

```bash
python3 report.py
```

### Extended period

```bash
python3 report.py --days 30
```

### Machine-readable (for dashboards or scripts)

```bash
python3 report.py --json
python3 report.py --csv
```

### Example output

```
======================================================================
  ENGRAM EFFECTIVENESS REPORT — Last 7 days
  Generated: 2026-06-11 18:51
======================================================================

  MCP USAGE BY SERVER
  ------------------------------------------------------------------
  Server                           Calls    Hits  Misses  Hit Rate
  ------------------------------------------------------------------
  hindsight                           45      38       7     84.4%
  hindsight-docs                      32      28       4     87.5%
  hindsight-issues                    18      14       4     77.8%
  gopls                               67      63       4     94.0%
  ------------------------------------------------------------------
  TOTAL                              162     143      19     88.3%

  EFFECTIVENESS (Corrections Reduction)
  ------------------------------------------------------------------
  Sessions with recall:       12  (avg 0.83 corrections/session)
  Sessions without recall:     8  (avg 3.25 corrections/session)
  Estimated correction reduction: 74.5%

  RECALL PROBE QUALITY (Nightly Health Check)
  ------------------------------------------------------------------
  Bank                            Probes  Avg Latency  Avg Results
  ------------------------------------------------------------------
  cursor-memory                        7      850ms         22.3
  kubernaut-docs                      14      1200ms        31.5
  kubernaut-issues                     7      1900ms         9.0

  MENTAL MODELS
  ------------------------------------------------------------------
  Bank                      Model                      Content    Refreshed
  ------------------------------------------------------------------
  cursor-memory             coding-conventions          5838 ch   2026-06-12
  cursor-memory             testing-methodology         8236 ch   2026-06-12
  kubernaut-docs            ka-architecture             9937 ch   2026-06-12
  kubernaut-issues          active-priorities           8501 ch   2026-06-13
  ------------------------------------------------------------------
  Total synthesized knowledge: 94,174 characters across 9 models
======================================================================
```

## Interpreting Results

### Healthy indicators

- **Hit rate > 70%** for hindsight banks (recall is finding relevant memories)
- **Hit rate > 90%** for gopls (type queries should almost always succeed)
- **Correction reduction > 30%** after 2+ weeks of data
- **Recall latency < 2000ms** (local embeddings should be fast)

### Warning signs

- **Hit rate < 50%**: Queries may be too broad or bank content is stale
- **Correction rate increasing**: New patterns not being captured — check nightly logs
- **Latency > 5000ms**: Database may need optimization or bank is too large
- **Zero gopls calls**: Agent may not be using code intelligence — check rule

### Actions

- **Low hit rate on hindsight-docs**: Re-run `ingest-docs.py` after doc updates
- **Low hit rate on hindsight-issues**: Re-run `ingest-issues.py` or check `gh auth status`
- **High corrections with recall active**: Retained patterns may be outdated — run reflect manually
- **Mental models stale**: Run `python3 create-mental-models.py --refresh` to force refresh
- **gopls not being used**: Verify `~/.cursor/mcp.json` has the gopls entry and restart Cursor

## Log File Locations

| File | Content | Written by |
|------|---------|-----------|
| `~/.hindsight/logs/mcp-calls.jsonl` | Real-time MCP call log | Cursor hook |
| `~/.hindsight/logs/effectiveness-report.jsonl` | Daily effectiveness metrics | Nightly script |
| `~/.hindsight/logs/recall-signals.jsonl` | Bank stats + recall probes | Nightly script |
| `~/.hindsight/logs/YYYY-MM-DD.json` | Full daily report | Nightly script |

## Setup

Install the monitoring hook (the `hooks.json` template uses `__HOME__` which gets
resolved to your home directory):

```bash
mkdir -p ~/.cursor/hooks
cp cursor/hooks/log-mcp-calls.sh ~/.cursor/hooks/
chmod +x ~/.cursor/hooks/log-mcp-calls.sh
sed "s|__HOME__|$HOME|g" cursor/hooks.json > ~/.cursor/hooks.json
```

Restart Cursor to activate the hook.
