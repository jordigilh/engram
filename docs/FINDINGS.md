# Research Findings

Historical record of empirical findings from running Engram in production.

## 2026-07-03: Production Hindsight Outage — Leaked Test DB Advanced Prod Migrations

**Context**: The daily 3pm `pkill -f hindsight-api` restart (see 2026-06-26 entry
below) killed the service as scheduled, but it then crash-looped indefinitely on
restart — `KeepAlive: true` respawned it every ~5 seconds, hitting the same fatal
error each time. All Hindsight MCPs (recall/retain) were down machine-wide until
fixed.

**Root cause**: `hindsight-api`'s embedded Postgres (`pg0`) resolves the sentinel
value `"pg0"` to a named instance under `~/.pg0/instances/<name>/`, defaulting to
`name="hindsight"` — the exact same name/data directory the production service
uses on port 5432. While investigating an unrelated deadlock bug in a forked
`hindsight-api-slim` checkout (`~/go/src/github.com/jordigilh/hindsight`), some
dev/test invocation ran without an explicit isolated instance name, attached to
the already-running production Postgres, and ran `alembic upgrade head` using the
fork's checkout — which was ~10 migrations ahead of the pip-installed production
package (`hindsight-api` 0.8.1). This stamped `alembic_version` in the production
DB to a revision (`b57a7c9e0d13`) that 0.8.1's migration chain didn't recognize.
Every subsequent startup failed with `alembic.util.exc.CommandError: Can't locate
revision identified by 'b57a7c9e0d13'` → `RuntimeError: Database migration
failed` → `Application startup failed. Exiting.` This had been silently true for
days — it only surfaced once the process was actually restarted (via the 3pm job).

**Fixes applied**:

1. **Unloaded the crash-looping launchd service** immediately to stop the
   respawn loop (`launchctl unload io.vectorize.hindsight.service.plist`).
2. **Verified the fix target**: downloaded and inspected the latest PyPI wheel
   for `hindsight-api-slim` (0.8.4, three releases ahead of the installed 0.8.1)
   and confirmed it contains the missing migration (`b57a7c9e0d13`) and matches
   the fork's migration count exactly — i.e. the production DB's schema was
   already fully consistent with an *officially released* version, just not
   the one installed.
3. **Upgraded via the documented runbook**: `uv pip install --python
   ~/.hindsight/venv/bin/python -U 'hindsight-api[all]'`, then reloaded the
   service. Migration check passed immediately; `/health` returned healthy.
4. **Cleaned up 6 leaked embedded-Postgres test instances** (`hindsight-test`,
   `hindsight-vecidx-test`, `hindsight-backsweep-test`, `hindsight-long-bankid-
   test`, `hindsight-remaining-bankid-test`, `hindsight-obs-sv-backfill-test`)
   that had been running unattended since the prior weekend's full pytest run —
   ~1GB of leaked disk + idle processes, unrelated to the outage but discovered
   during triage.
5. **Fixed the actual trigger**: `io.vectorize.hindsight.restart.plist`'s daily
   restart was still scheduled for 3pm despite an earlier decision to move it to
   1am — that reschedule had never been applied (the plist lives only in
   `~/Library/LaunchAgents/`, untracked by git, so the decision had no durable
   record and silently reverted/never landed). Rescheduled to 1am and added the
   plist to `launchd/` in this repo so future schedule decisions survive.

**Takeaways**:
- **Never point dev/test tooling at a shared default resource name.** When
  working in ad hoc/manual sessions against a forked service (not the pytest
  suite, which correctly isolates via named instances), always pass an explicit
  `HINDSIGHT_API_DATABASE_URL` (or equivalent) that cannot collide with the
  production instance name, even for "just checking something quickly."
- **A migration mismatch fails silently until next restart.** A service that
  never restarts can carry a corrupted/ahead-of-code DB state indefinitely
  without any symptom, then fail 100% on the next restart. Consider a periodic
  health check that actually exercises restart-sensitive paths, or a migration-
  drift check independent of the daily restart.
- **launchd plists that aren't checked into the repo are not durable decisions.**
  If it's not in `launchd/` and referenced in setup docs, it will silently
  regress the next time someone (person or agent) "fixes" it. All operational
  schedule changes should be committed, not just applied live.

---

## 2026-06-26: Hindsight API Memory Leak — 17GB in 5 Days

**Context**: The `hindsight-api` process (PID 1346) had been running since Monday
and accumulated 17GB of dirty memory (peaked at 19GB) on a MacBook with Apple
Silicon. The machine was noticeably slower.

**Memory breakdown:**

| Category | Dirty Memory | Cause |
|----------|----------:|-------|
| IOAccelerator (graphics) | 9,358 MB | GPU memory from local embedding + reranker models via Metal |
| MALLOC_SMALL | 3,425 MB | Heap growth from connection pools, caches |
| MALLOC_NANO | 3,217 MB | Heap growth from Python object fragmentation |
| VM_ALLOCATE | 746 MB | Generic virtual memory |
| MALLOC_TINY | 491 MB | Small allocations |
| MALLOC_MEDIUM | 119 MB | Medium allocations |
| **Total** | **~17 GB** | |

**Root causes:**

1. **Local ML models on GPU (9.3GB)**: The embedding model (`BAAI/bge-small-en-v1.5`,
   33M params) and cross-encoder reranker (`cross-encoder/ms-marco-MiniLM-L-6-v2`,
   22M params) were running on Apple Silicon GPU via Metal. Metal's IOAccelerator
   allocates large contiguous GPU buffers and does not release them. These are small
   models that don't benefit meaningfully from GPU acceleration — the Metal overhead
   dominates any inference speedup.

2. **Oversized DB connection pool (6.6GB heap)**: Default pool was min=5 / max=100
   asyncpg connections. For a single-user local deployment, this is ~10x more than
   needed. Each connection holds buffers; over 5 days the heap grew unbounded.

3. **Python heap fragmentation**: Long-lived Python processes accumulate fragmented
   memory that the OS never reclaims even after Python's GC frees objects. This is a
   known CPython behavior with no fix other than periodic restarts.

**Fixes applied:**

1. **Force CPU mode** for both models:
   - `HINDSIGHT_API_EMBEDDINGS_LOCAL_FORCE_CPU=true`
   - `HINDSIGHT_API_RERANKER_LOCAL_FORCE_CPU=true`
   - Eliminates the 9.3GB GPU allocation entirely

2. **Shrink DB pool** to match single-user usage:
   - `HINDSIGHT_API_DB_POOL_MIN_SIZE=2`
   - `HINDSIGHT_API_DB_POOL_MAX_SIZE=10`

3. **Daily restart at 3pm** via launchd (`io.vectorize.hindsight.restart.plist`):
   - Sends `pkill -f hindsight-api`; `KeepAlive: true` restarts it within 5 seconds
   - Reclaims any heap fragmentation before it accumulates

**Results after restart with new config:**

| Metric | Before | After | Change |
|--------|-------:|------:|-------:|
| RSS memory | 17,000 MB | 1,077 MB | **-94%** |
| cursor-memory recall | 2,444 ms | 1,459 ms | **-40%** |
| kubernaut-docs recall | 13,987 ms | 3,252 ms | **-77%** |

CPU mode was not only smaller but *faster* — Apple Silicon CPU cores avoid the
Metal/IOAccelerator overhead for these small models. The GPU pathway adds
serialization and buffer management cost that exceeds the compute speedup for
models under ~100M parameters.

**Lessons:**
1. **GPU is not always faster** — for small models (<100M params) on Apple Silicon,
   CPU inference can be faster due to Metal buffer management overhead.
2. **Default pool sizes are for multi-tenant SaaS** — a single-user local deployment
   should use min=2/max=10, not min=5/max=100.
3. **Long-lived Python processes need periodic restarts** — CPython heap fragmentation
   is inevitable; a daily restart is the practical solution.
4. **Monitor process memory** — this went unnoticed for 5 days. A periodic memory
   check in the nightly pipeline would have caught it sooner.

---

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
