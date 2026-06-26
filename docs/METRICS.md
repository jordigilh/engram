# Metrics and Effectiveness Monitoring

## Overview

Engram tracks four categories of metrics to evaluate whether the memory system
reduces mistakes and improves productivity:

1. **MCP Usage** — How often each tool is called and whether it returns useful results
2. **Effectiveness** — Correlation between recall usage and correction rates
3. **Proactive Recall** — Whether the agent uses memory autonomously without user prompting
4. **Recall Quality** — Latency and result counts from nightly health probes

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
- Proactive recall rate (sessions where agent recalls without user prompting)
- Per-turn recall density
- Session length proxy (message count)

## Metrics Definitions

| Metric | Formula | What it measures |
|--------|---------|-----------------|
| **Hit Rate** | hits / total_calls | Retrieval quality — are queries returning relevant content? |
| **Correction Rate** | corrections / sessions | How often you need to correct the assistant |
| **Reduction %** | 1 - (rate_with / rate_without) | Improvement from memory: fewer corrections when recall is active |
| **Recall Adoption %** | sessions_with_recall / total_sessions | What fraction of sessions use memory at all |
| **Proactive Recall %** | proactive_sessions / total_sessions | Sessions where agent recalled without user mentioning memory |
| **Per-turn Recall %** | turns_with_recall / total_turns | Density of recall usage within sessions |
| **Context Loading Cost** | chars_before_first_productive_action / 4 | Tokens consumed to orient the agent before real work starts |
| **Productivity Density** | productive_actions / (total_tokens / 1000) | Productive actions per 1K tokens — higher is better |
| **Rework Ratio** | rework_tokens / total_tokens | Fraction of tokens wasted on correction loops — lower is better |
| **Rework %** | rework_ratio × 100 | Same as rework ratio, expressed as percentage |
| **Rework Tokens** | Σ(post-correction segment / 2) | Estimated tokens wasted redoing work after each user correction |
| **First Productive Turn** | Turn index of first productive action | How quickly the agent starts real work — lower is better |
| **Recall Latency** | ms per recall call | Performance health — should be <2s for good UX |
| **Result Count** | chunks returned per recall | Coverage — more results = richer context |
| **Triage Flagged %** | flagged / total_memories | Memory hygiene: what fraction of stored memories is noise |
| **Triage Deletable** | documents where all memories are flagged | Cleanup yield: documents safe to remove entirely |

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

  PROACTIVE RECALL (Is the agent using memory without being asked?)
  ------------------------------------------------------------------
  Recall adoption:     60.0% of sessions use recall
  Proactive recall:    45.0% of sessions recall without user prompting
  Per-turn recall:      1.2% of agent turns include a recall call

  Healthy: Agent is proactively recalling in most sessions.

  SESSION DISTRIBUTION (How sessions break down by size)
  ------------------------------------------------------------------
  Bucket              With Recall    Without Recall     Total
  ------------------------------------------------------------------
  Trivial (<5K)                 5                 3         8
  Small (5-15K)                 4                 2         6
  Medium (15-100K)              6                 1         7
  Large (>100K)                 3                 0         3
  ------------------------------------------------------------------

  RECALL SESSION STATS (Non-trivial sessions with recall)
  ------------------------------------------------------------------
  Sessions:                   13
  Corrections/session:      0.83
  Rework %:                  4.2%
  Productivity density:    0.3100  (productive actions per 1K tokens)
  First productive turn:     2.3

  WEEKLY TREND (Recall sessions only, from epoch)
  ------------------------------------------------------------------
  Epoch: 2026-06-26 (0 days ago)
  Stabilization window: 7 days remaining

  Week         Sessions  Corr/Sess  Rework%  ProdDensity   1st Prod
  ------------------------------------------------------------------
  2026-W26           13       0.83     4.2%       0.3100        2.3
  ------------------------------------------------------------------

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
- **Recall adoption > 50%**: The agent is using memory in most sessions
- **Proactive recall > 30%**: The agent initiates recall without user prompting
- **Rework % < 10%**: Less than 10% of tokens are wasted on correction loops
- **Productivity density trending up**: More productive actions per token over time
- **First productive turn < 3**: Agent starts real work quickly with recalled context
- **Recall latency < 2000ms** (local embeddings should be fast)

### Warning signs

- **Hit rate < 50%**: Queries may be too broad or bank content is stale
- **Correction rate increasing**: New patterns not being captured — check nightly logs
- **Recall adoption < 30%**: The Cursor rule may not be triggering — check `alwaysApply` is set
- **Proactive recall 0%**: Agent only recalls when user explicitly asks — rule wording may need strengthening
- **Rework % > 20%**: Significant tokens going to correction loops — investigate content quality
- **Productivity density declining week over week**: Agent efficiency is degrading — check for content staleness
- **First productive turn > 5**: Agent is slow to start work — rule or recall content may need improvement
- **Latency > 5000ms**: Database may need optimization or bank is too large
- **Zero gopls calls**: Agent may not be using code intelligence — check rule

### Actions

- **Low hit rate on hindsight-docs**: Check CocoIndex flow status; for manual recovery, run `ingest-docs.py`
- **Low hit rate on hindsight-issues**: Check CocoIndex flow status; for manual recovery, run `ingest-issues.py` or check `gh auth status`
- **High corrections with recall active**: Retained patterns may be outdated — run reflect manually
- **Mental models stale**: Run `python3 create-mental-models.py --refresh` to force refresh
- **Low proactive recall**: Strengthen the `alwaysApply` rule wording, ensure it says "ALWAYS recall before starting work"
- **gopls not being used**: Verify `~/.cursor/mcp.json` has the gopls entry and restart Cursor

## Memory Triage

The nightly pipeline includes a triage phase that identifies and removes low-value
memories to keep the knowledge graph clean and retrieval relevant.

### What gets flagged

| Category | Description |
|----------|-------------|
| **Ephemeral** | Assistant/user action narration, CI status, build results — transient facts with no lasting value |
| **Snapshot** | Point-in-time state ("running v1.4.0-rc3", "waiting for X") that becomes stale |
| **Short** | Memories under 80 characters that lack sufficient context |
| **Near-duplicate** | Memories with >85% text similarity (keeps the newer one) |
| **Repeated-fact** | The same factual claim restated 3+ times (keeps the most recent) |
| **Stale** | Flagged memories older than 14 days |

Memories containing valuable patterns (architecture decisions, ADRs, conventions,
root-cause explanations) are protected from classification even if they match
ephemeral patterns.

### Rearrange strategy

Hindsight stores memories grouped under documents. The triage script handles
both fully-flagged and mixed documents:

- **Fully flagged documents** (all memories are noise): deleted outright.
- **Mixed documents** (some flagged, some valuable): *rearranged* — the
  original document is deleted and only the valuable memories are re-retained
  using `strategy: 'exact'` (verbatim storage, no LLM re-extraction cost).
  Each re-retained memory gets a unique `document_id` to satisfy the batch API
  constraint (no duplicate `document_id` values per batch).

### Running manually

```bash
# Dry-run (report only)
python3 triage-memories.py

# Apply deletions
python3 triage-memories.py --apply

# JSON output for scripting
python3 triage-memories.py --json

# Adjust stale threshold
python3 triage-memories.py --stale-days 7
```

### Recovery after data loss

If triage (or any operation) causes unexpected memory loss, use the recovery
script to rebuild the bank from transcripts:

```bash
# Dry-run: show how many windows would be re-extracted
python3 recover-memories.py

# Apply: reset watermarks, reprocess all transcripts
python3 recover-memories.py --apply

# Limit to last N days of transcripts
python3 recover-memories.py --apply --max-age 30
```

The recovery script backs up `watermarks.json` and `retained-hashes.json`
before resetting them, then re-extracts all corrections and instructions via
the normal Haiku pipeline. Watermarks are restored after recovery so the
nightly pipeline doesn't double-process.

### Healthy indicators

- **Flagged % decreasing over time**: The retain pipeline is producing cleaner content
- **Deletable docs per run < 5**: Most noise is mixed with valuable content (expected)
- **Ephemeral count stable or declining**: The LLM extraction is learning to skip narration
- **Re-retained count matches kept count**: All valuable memories survived rearrange

### Warning signs

- **Re-retained < kept**: Some re-retain batches failed — check for API errors in logs
- **Memory count drops sharply after triage**: Rearrange may have failed — run `recover-memories.py`

### Triage log

Results are appended to `~/.hindsight/logs/triage-report.jsonl` with per-run breakdowns.

## Log File Locations

| File | Content | Written by |
|------|---------|-----------|
| `~/.hindsight/logs/mcp-calls.jsonl` | Real-time MCP call log | Cursor hook |
| `~/.hindsight/logs/effectiveness-report.jsonl` | Daily effectiveness metrics | Nightly script |
| `~/.hindsight/logs/recall-signals.jsonl` | Bank stats + recall probes | Nightly script |
| `~/.hindsight/logs/triage-report.jsonl` | Memory triage results | Nightly script |
| `~/.hindsight/logs/YYYY-MM-DD.json` | Full daily report | Nightly script |

## CocoIndex-Aware Metrics

With CocoIndex integration, three additional metric dimensions become available.

### Hybrid Search Quality

The code index uses hybrid search (dense + BM25 via RRF fusion). Track search
mode effectiveness to understand which retrieval method contributes most:

| Mode | Best for | Watch for |
|------|----------|-----------|
| `hybrid` (default) | General queries mixing concepts and identifiers | Should outperform either mode alone |
| `dense` | "How does X work?" — conceptual, natural-language queries | Low scores = embedding model may need upgrade |
| `bm25` | "ParseConfig" — exact identifier lookup | No results = tsvector may not be populated (check trigger) |

To compare modes manually:

```bash
python3 cocoindex-search.py --query "reconciler error handling" --mode hybrid
python3 cocoindex-search.py --query "reconciler error handling" --mode dense
python3 cocoindex-search.py --query "ParseConfig" --mode bm25
```

**Healthy indicators:**
- Hybrid mode returns more diverse results than either mode alone
- BM25 mode finds exact identifiers that dense mode misses
- Dense mode finds conceptually related code that BM25 misses

**Warning signs:**
- BM25 returns 0 results for known identifiers: the `search_vector` trigger
  may not be firing — check `SELECT count(*) FROM cocoindex.code_embeddings WHERE search_vector IS NULL`
- Hybrid results identical to dense-only: BM25 index may be empty — re-run
  `python3 cocoindex-flows.py --mode backfill`

### Freshness-at-Recall

`avg_staleness_hours` measures the average age of content at the time it is
recalled. With CocoIndex continuously syncing sources, staleness should be
significantly lower than with batch ingestion.

| Source | Target Staleness | Previous (batch) |
|--------|-----------------|------------------|
| Docs | < 1 hour | Manual (unbounded) |
| Issues + PRs | < 5 minutes | ~24 hours (nightly, 500 cap) |
| Code | < 5 minutes | Not indexed |
| Transcripts | < 1 hour | ~24 hours (nightly) |

**Warning signs:**
- `avg_staleness_hours` > 24 for docs/issues: CocoIndex may not be running — check `launchctl list | grep cocoindex`
- `avg_staleness_hours` > 1 for code: delta processing may be stalled — check `~/.hindsight/logs/cocoindex-stdout.log`

### Exploration Efficiency

Measures how quickly the agent finds relevant code context. Without code
indexing, the agent relies on `gopls` symbol lookups and `SemanticSearch` — both
effective but limited to known entry points. The code index provides semantic
search across the full codebase, reducing the number of tool calls needed to
locate unfamiliar code.

| Metric | Formula | What it measures |
|--------|---------|-----------------|
| **Exploration calls/task** | search_tool_calls / tasks | How many lookups to find relevant code |
| **First-hit depth** | turns_before_first_relevant_code | How quickly the agent reaches useful code |

**Healthy indicators:**
- Exploration calls/task decreasing over time (code index is covering more queries)
- First-hit depth < 2 turns (code index returns relevant results on first query)

**Warning signs:**
- Exploration calls/task increasing: code index may not be covering the queried area — check if the source directory is configured
- Code index hit rate < 50%: embeddings may need reprocessing — run `python3 cocoindex-flows.py --mode backfill`

## Exploration Efficiency

Measures whether recall replaces grep/glob/SemanticSearch exploration calls.
Computed per session in the nightly pipeline and surfaced in `report.py`.

| Metric | Formula | What it measures |
|--------|---------|-----------------|
| **Exploration calls before productive** | grep/glob/SemanticSearch calls before first Write/Shell | How many search operations to reach useful work |
| **Exploration delta** | 1 - (with_recall / without_recall) | % reduction in exploration from recall |
| **CocoIndex code search impact** | with_cocoindex vs without_cocoindex | Whether semantic code search reduces exploration further |

**Healthy indicators:**
- Exploration calls before productive < 2 with recall (recall front-loads context)
- Exploration delta > 50% (recall halves the search overhead)
- CocoIndex sessions show fewer exploration calls than non-CocoIndex sessions

**Warning signs:**
- Exploration calls increasing with recall: content may not match query patterns
- No difference between CocoIndex/non-CocoIndex: code index may not cover queried areas

## Ingestion Coverage

Tracks what percentage of each source is actually indexed. Computed live by
`report.py` by comparing GitHub CLI counts with Hindsight bank document counts.

**Healthy indicators:**
- Issues + PRs coverage at 100% (all items indexed)
- Docs coverage matching file count on disk

**Warning signs:**
- Coverage < 100%: the `--limit` cap may be too low, or ingestion errors are
  silently dropping items

## Baseline Comparison

Use `report.py --snapshot` to capture a baseline, and `report.py --compare <file>`
to see deltas over time. Key metrics to watch:

- Exploration calls delta (are we needing fewer searches?)
- Correction rate delta (are corrections declining?)
- Rework % trends (is the agent spending less time on correction loops?)
- Productivity density trends (are more productive actions produced per token?)

```bash
# Take a baseline before making changes
python3 report.py --snapshot

# Compare after a week
python3 report.py --compare ~/.hindsight/logs/baseline-2026-06-22.json
```

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
