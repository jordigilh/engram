# Research Findings

Historical record of empirical findings from running Engram in production.

## 2026-06-26: Retire K-score and NES — Replace with Weekly Trend Metrics

**Context**: After two weeks of collecting K-score (token efficiency multiplier)
and NES (Net Efficiency Score / rework avoidance), we identified structural
problems that made both metrics unreliable for tracking Engram's effectiveness.

**Problem: selection bias between cohorts**

K-score and NES compare sessions *with recall* against sessions *without recall*.
This comparison is fundamentally flawed because:

1. Sessions without recall are self-selecting — they tend to be trivial one-off
   commands, quick fixes, or simple questions that don't trigger the rule.
2. Sessions with recall are self-selecting — they tend to be complex multi-step
   tasks where the agent engages deeply with the codebase.
3. These are different *types* of work, not the same work done with/without a tool.

The result: K-score and NES fluctuated wildly day to day (from 0.5x to 2.5x)
depending on the mix of session types, not on Engram's actual effectiveness.
A day with many trivial no-recall sessions would show high K-score (recall
sessions look great by comparison); a day with only complex recall sessions
would show low K-score (no baseline to compare against).

**Additional factor**: The June 19 rule rewrite added mandatory planning gates
and mid-session re-recall, significantly increasing recall frequency. This meant
even more sessions would use recall, further shrinking the "without recall"
control group and making the comparison even less stable.

**Solution: within-cohort weekly trend metrics**

Instead of comparing two structurally different cohorts, track the *same cohort*
(recall sessions) over time. Week-over-week trends within a single population
are immune to selection bias.

New metrics (all computed on non-trivial recall sessions only):

| Metric | Formula | What it measures |
|--------|---------|-----------------|
| Corrections/session | corrections / sessions | Error rate (lower is better) |
| Rework % | rework_tokens / total_tokens | Waste rate (lower is better) |
| Productivity density | productive_actions / (tokens / 1000) | Efficiency (higher is better) |
| First productive turn | avg turn of first productive action | Ramp-up speed (lower is better) |

**Other changes in this epoch:**

1. **New bucket thresholds**: Trivial (<5K), Small (5-15K), Medium (15-100K),
   Large (>100K). Previous thresholds (50K/500K) were too coarse — most sessions
   clustered in "small" while meaningful work happened between 15-100K tokens.
   Added a "trivial" bucket to explicitly exclude sessions that are too short
   to measure (auto-completions, one-shot questions).

2. **Session distribution diagnostic**: Raw counts per bucket with/without recall,
   so empty buckets are immediately visible rather than silently producing no data.

3. **Epoch boundary**: June 26, 2026. All weekly trends start from this date.
   Data collected before the epoch used different rules, different bucket
   thresholds, and different metrics — it is not comparable and is archived
   but not displayed.

4. **Per-session fields**: `productivity_density` and `rework_ratio` computed
   per session and stored in the nightly report for downstream aggregation.

**What was removed:**
- K-score (global, per-bucket, per-bank, normalized)
- NES (global, per-bucket, NES ratio)
- `k_curve` and `net_efficiency_score` sections from nightly report output
- Per-bank K-score effectiveness breakdown

**What was kept:**
- MCP usage and hit rates (operational health, not effectiveness measurement)
- Proactive recall metrics (measures agent behavior, not session comparison)
- Exploration efficiency (with/without recall comparison, but less sensitive to
  selection bias because exploration call count is relatively stable across
  session types)
- Correction reduction % (simple and interpretable, even if noisy)

**Lessons:**
1. **Metrics that compare self-selected groups are structurally biased.** The
   with/without recall split is not a controlled experiment — it's an
   observational study with confounders (session complexity, task type, user
   behavior). Within-cohort trends avoid this entirely.
2. **Volatile daily metrics need weekly smoothing.** Any daily metric with <20
   sessions will be dominated by random variation. Weekly cohorts provide enough
   sample size for meaningful trends.
3. **Epoch boundaries matter.** When system parameters change significantly
   (rules, thresholds, recall triggers), old data becomes non-comparable.
   Declaring a clean epoch and starting fresh is better than trying to normalize
   across incompatible configurations.

---

## 2026-06-20: Memory Triage Incident — Batch document_id Bug

**Context**: Implemented a memory triage system to automatically clean low-value
memories (ephemeral narration, stale snapshots, near-duplicates) from the
knowledge graph as part of the nightly pipeline.

The triage uses a "rearrange" strategy for mixed documents (containing both
valuable and flagged memories): delete the original document, then re-retain
only the valuable memories using `strategy: 'exact'` (verbatim storage, no LLM
re-extraction cost).

**Bug**: The `rearrange_document` function assigned the same `document_id` to
every item in a re-retain batch. The Hindsight API rejects batches with
duplicate `document_id` values to prevent race conditions. This caused all
multi-item re-retain batches to fail with HTTP 400.

**Impact**:
- Pre-triage: 2,620 memories
- Expected post-triage: ~2,138 (removing 482 flagged)
- Actual post-triage: **420 memories** (1,718 valuable memories lost)
- The 148 mixed documents were deleted successfully, but their valuable memories
  were not re-retained due to the batch failures
- 80 clean documents (untouched) and 36 single-item re-retains survived

**Root cause**: Each item in a batch must have a unique `document_id`. The code
used a single UUID for the entire document rather than per-item UUIDs.

**Fix**: Changed `rearrange_document` to generate a unique `document_id` per
item using `f"{doc_prefix}-{uuid.uuid4().hex[:8]}"`.

**Recovery**: Created `recover-memories.py` to reprocess all 343 transcripts:
1. Reset watermarks.json and retained-hashes.json (with backups)
2. Scanned all 343 transcripts — 87 had learning signals
3. Re-extracted 475 learning windows (175 corrections + 300 instructions)
4. Retained 394 windows (81 skipped as duplicates), zero errors
5. Memory count recovered from **420 → 1,625** (~62% of original 2,620)
6. Recovery took ~29 minutes (Haiku extraction via Vertex AI)
7. Watermarks restored after recovery to prevent nightly double-processing

The 38% gap (2,620 → 1,625) is expected: many of the original 2,620 memories
were the flagged noise (482) plus memories from older transcripts that aged
out of the scan window or from reflect/consolidation operations that aren't
re-triggered by transcript reprocessing alone. The mental model refresh in the
next nightly run will synthesize the recovered facts into coherent documents.

**Lessons**:
1. **Always dry-run destructive operations end-to-end** — the dry-run correctly
   identified flagged memories but didn't exercise the re-retain path.
2. **Delete after re-retain, not before** — the rearrange should verify
   re-retain success before deleting the original document. Future improvement.
3. **The recovery pipeline is a key safety net** — because transcripts are the
   source of truth and are retained on disk, memory banks can always be rebuilt
   from scratch. This is an inherent advantage of the architecture.
4. **Batch API constraints must be tested with real payloads** — the
   `strategy: 'exact'` API was untested before the live run.

---

## 2026-06-20: Net Efficiency Score and Session Length Strategy

**Context**: After implementing K-score normalization by session size, we needed a
metric that captures rework avoidance — the tokens saved by preventing correction
loops, which K-score alone does not measure.

**New metric**: Net Efficiency Score (NES) = (total_tokens - rework_tokens) / total_tokens

Rework tokens are estimated by tracking the character position of each user
correction and attributing half of the subsequent segment (until the next correction
or session end) as rework cost.

**Results (7-day window, 151 transcripts)**:

| Metric | With Recall | Without Recall | Delta |
|--------|:-:|:-:|:-:|
| NES | 0.882 | 0.640 | +38% |
| Avg rework tokens | 9,032 | 71,339 | -87% |
| Avg total tokens | 76,844 | 197,902 | -61% |

**NES ratio: 1.38x** — sessions with recall waste 38% fewer tokens on rework.

### Session Length Analysis

| Bucket | Sessions (R / no-R) | NES (R) | NES (no-R) | Ratio | Rework% (R) | Rework% (no-R) |
|--------|:-:|:-:|:-:|:-:|:-:|:-:|
| Small (10-50K) | 19 / 3 | 0.989 | 1.000 | 0.99x | 1.1% | 0.0% |
| Medium (50-500K) | 23 / 4 | 0.925 | 1.000 | 0.93x | 7.5% | 0.0% |
| Large (>500K) | 3 / 4 | 0.799 | 0.605 | 1.32x | 20.1% | 39.5% |

**Key findings**:

1. **Large sessions benefit most from Engram for rework avoidance** — without recall,
   39.5% of tokens go to rework. With recall, that drops to 20.1% (NES ratio 1.32x).

2. **Small sessions show no NES benefit** (0.99x) — short focused sessions naturally
   avoid rework. Engram's value for short sessions is primarily in K-score (context
   front-loading) rather than rework prevention.

3. **The "without recall" small/medium buckets show 0% rework** — likely a sample size
   artifact (only 3-4 sessions without recall). These happened to be correction-free.

4. **Session strategy insight**: Short per-topic sessions are already optimized for
   rework avoidance. Engram's value for short sessions is the K-score benefit (skipping
   the education phase). For unavoidable long sessions, Engram provides significant
   rework protection.

### Caveats

- The 50% rework heuristic is a constant — real rework fraction varies by correction
  severity (typo fix vs architectural redo).
- Small sample size in "without recall" buckets limits statistical confidence.
- Rework estimation does not count the wasted work *before* the correction (the wrong
  implementation that triggered it).

---

## 2026-06-17: K-score Normalization by Session Size

**Context**: The raw K-score was biased because "without recall" sessions were
disproportionately large code-generation sessions, while "with recall" sessions
were shorter and focused.

**Solution**: Bucket sessions into Small (10-50K tokens), Medium (50-500K), and
Large (>500K). Compute K-score per bucket and weight by bucket size.

**Results**:
- Excluded sessions under 10K tokens (where recall overhead dominates the signal)
- Per-bucket K-scores provide fairer comparison between like-sized sessions
- Normalized K-score weights by total session count per bucket

**Takeaway**: Always normalize efficiency metrics by session size to avoid confounding
session complexity with tool effectiveness.

---

## 2026-06-15: Recall Is Not Happening Mid-Session

**Context**: After the initial recall at session start, the agent was not recalling
again during implementation phases — missing relevant methodologies (TDD, pyramid
invariant, FedRAMP tests) when they would have been most useful.

**Root cause**: The Cursor rule only triggered recall at session start. No guidance
existed for phase-based recall during implementation.

**Fix**: Updated `hindsight-memory.mdc` with explicit phase-based triggers:
- Implementation planning → recall testing methodology
- Writing tests → recall test conventions
- Designing APIs → recall API contracts
- PR/commit workflow → recall commit conventions
- Debugging → recall known bugs and past failures
- Pipeline monitoring → recall monitoring protocol
- Implementation complete → recall GA readiness audit

**Impact**: Phase-based triggers ensure the agent recalls domain-specific knowledge
at the moment it's needed, not just at session start.

---

## 2026-06-13: Hourly Retain Pipeline Reduces Memory Staleness

**Context**: The nightly-only pipeline meant corrections and instructions extracted
from transcripts could be up to 24 hours stale. A bug also caused duplicate entries
in the knowledge graph from repeated re-processing of the same corrections.

**Solution**: Hourly retain pipeline with:
- Watermark tracking (file size + message count + timestamp) to identify new content
- SHA-256 hash deduplication to prevent duplicate entries
- Two-layer filter: size gate + regex pre-filter before invoking Haiku extraction

**Impact**:
- Memory freshness improved from ~24h to ~1h
- Duplicate entries eliminated via hash-based dedup
- Nightly `dedup_graph` added as a safety net for any duplicates that slip through

---

## 2026-06-11: Initial Hypothesis Validation

**Hypothesis**: Engram reduces token consumption and increases effectiveness by
front-loading context from memory, avoiding the "education phase" at session start.

**Initial findings**:
- Correction reduction: ~74% fewer corrections in sessions with recall
- Context loading reduction: ~97% fewer tokens before first productive action
- K-score: 1.72x (recall sessions are 72% more token-efficient per productive action)

**Complication**: Total token consumption was *higher* in recall sessions. This
appeared to contradict the hypothesis until we identified that recall sessions were
also longer and more complex (selection bias). The K-score per-productive-action
metric confirmed the per-token efficiency gain even when total consumption rose.

**Takeaway**: Raw token totals are misleading. The correct metric is tokens per
productive action (effectiveness ratio), normalized by session size.
