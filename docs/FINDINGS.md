# Research Findings

Historical record of empirical findings from running Engram in production.

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
