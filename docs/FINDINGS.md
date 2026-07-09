# Research Findings

Historical record of empirical findings from running Engram in production.

## 2026-07-09: Haiku Correction Classifier Had 90%+ False Positive Rate — Prompt v2 Cuts It to ~10% With Zero Recall Regression

**Context**: With the Prefilter Shadow Trial running live (see the two 2026-07-08 spike entries below),
user asked to triage whether Haiku's `classify_correction` was itself over-flagging — specifically,
mislabeling clarifications, new task assignments, and open questions as "corrections" — and to tweak the
prompt if so, then keep updating this file daily with the ongoing evaluation.

**Triage method**: Pulled a random sample of 80 unique messages Haiku (v1 prompt) had flagged
`is_correction=true` from the live shadow trial log and manually read each one, judging genuine
correction vs. false positive independent of Haiku's own label.

**Result — v1 prompt false positive rate: ~42.5% (34/80)**. This wasn't a handful of ambiguous edge
cases; it was entire categories the v1 prompt's negative examples never anticipated, all sharing a common
shape: imperative or declarative phrasing that *sounds* instructional/critical without actually asserting
the assistant did anything wrong. Four recurring patterns:
- **New task/plan assignment**: "implement the plan as specified", "commit in logical groups and create a
  PR", "add integration tests for both gateways" — assigning new work, not correcting prior work.
- **Forward-looking requirement/scope statement**: "leave them for amd64 only", "we should have ITs for
  both gateways", "I'd rather have it phased like X" — a new decision, no implied prior wrongdoing.
- **Open design question**: "why not a simple regex?", "can we organize it better?", "should we have a
  dedicated memory bank or consolidate?" — genuine questions, not assertions that something is wrong.
- **TODO/status-check reminder**: "you will still have to add jordigilh to cspell.yaml", "check that we're
  using the correct context" — pending work, not a claim the assistant already got it wrong.

**Fix applied**: Rewrote `_CORRECTION_SYSTEM_PROMPT` in `spike/classify.py` (v1 → v2) with explicit
negative examples for each of the four patterns above, plus a stricter framing ("must assign fault to
something the assistant ALREADY did or said") and an explicit false-negative-is-cheaper-than-false-positive
tie-break for genuine ambiguity.

**First validation pass surfaced a real regression, caught before it shipped**: Re-running v2 against
`ground_truth.py`'s 19-example held-out eval split (never used to write either prompt) showed recall
dropping from 0.93 (v1) to 0.73 — v2 was now missing "do not use patent search engine" and "we don't use
env variables"-style convention violations. Root cause: v2's new "new task/requirement statement" negative
examples used imperative phrasing that pattern-matched too broadly against "we don't use X" / "do not use
X" corrections, which are *also* phrased as forward directives despite being genuine convention-violation
corrections (the exact category the original `CORRECTION_PATTERNS` regex fix on 2026-07-08 was written to
catch). **Fixed by adding an explicit carve-out**: "we don't use X" / "do not use X" / "that's not how we
do it" phrasing is called out as a correction regardless of imperative shape, with an explicit note that
this exception overrides both the "new task assignment" rule and the ambiguity tie-break.

**Final validation (v2 with carve-out)**:

| Test | v1 | v2 |
|---|---|---|
| Ground truth eval split (15 corrections, 4 benign, held-out) | recall 0.93, precision 1.00, F1 0.97 | recall 0.93, precision 1.00, F1 0.97 (identical — same single pre-existing miss both versions) |
| Live-traffic false-positive sample (30 messages, all human-judged non-corrections that v1 flagged) | 28/30 (93%) still flagged | 3/30 (10%) still flagged |

Zero regression on the original hand-labeled ground truth; the live false-positive rate dropped by
~90 percentage points. The 3 residual false positives under v2 are themselves genuinely borderline
(e.g. "we won't be using goose here, agents will be packaged as OCI..." — legitimately ambiguous with the
"we don't use X" carve-out given Haiku sees the message in isolation, with no preceding assistant turn to
confirm whether goose was actually proposed/used) — chasing them further risked re-introducing the same
regression just fixed, so v2 was kept as final for now.

**Corrected shadow-trial estimate**: Re-ran v2 against all 659 messages the v1 prompt had flagged as
corrections in the shadow trial log (540 unique texts, weighted back by original frequency): v2 confirms
only 270/659 (41.0%), excluding 389/659 (59.0%) as false positives — consistent with the ~42.5% rate found
in the manual 80-message triage. Applied against the full 4,045-message trial window, this revises the
estimated true correction rate from the previously reported 16.3% (630/3,873, using v1) down to **~6.7%
(270/4,045)**. This is a large downward revision of an already-large upward revision (the original
hand-curated 7-day scan assumed ~1-2/day); both directions of that arc reinforce the same lesson below.

**Not yet done**: The corrected v2 rate above is a point-in-time re-classification of already-collected v1
verdicts, not a live re-run of the trial — `prefilter-shadow-trial.py` itself hasn't been updated to call
v2 yet (it currently imports `classify_correction` from `spike/classify.py`, so it will pick up v2
automatically on its next scheduled run; no separate wiring needed, but this hasn't been confirmed against
a fresh run yet). The two prefilter candidates (`loose_regex_prefilter`,
`trivial_message_exclusion_filter`) were evaluated against v1's verdicts in the 2026-07-08 shadow-trial
entry below; their recall/reduction numbers should be treated as provisional until re-checked against v2's
corrected "confirmed correction" set, since the composition of what counts as a true correction just
changed materially.

**Takeaways**:
- **A classifier's own false-positive rate needs the same "does this number look plausible" scrutiny as
  any other metric in this system.** The shadow trial was built specifically to get a non-circular recall
  number for prefilters *against Haiku's verdicts* — but nothing was validating Haiku's verdicts
  themselves until this triage. A classifier can be simultaneously "the best available reference" (0.97 F1
  against held-out ground truth) and still wrong 40%+ of the time on a different, wider distribution of
  real traffic that the ground truth sample didn't fully represent.
- **Negative examples in a classification prompt can silently cannibalize a positive category that
  overlaps in surface phrasing.** "Do not use X" is simultaneously the shape of a brand-new forward
  directive (not a correction) and the shape of a convention-violation correction — v2's fix for the
  former accidentally broke the latter until an explicit carve-out was added and re-validated against held-
  out data. Any prompt change that adds negative examples should be checked for exactly this kind of
  overlap with existing positive categories, not just checked for whether it fixes the false positives it
  was written for.
- **Validate a prompt/classifier change against both the original ground truth AND the specific failure
  sample that motivated the change, every time** — checking only one side would have missed either the
  regression (ground truth) or the fix (false-positive sample) in this case.

## 2026-07-09: `report.py` Was Still Silently Blending Kubernaut + DCM, Despite the Earlier Scoping Fix

**Context**: User asked for last night's report for both projects. Running `report.py` produced
a single, unlabeled report with no visible way to separate the two projects.

**Root cause**: The 2026-07-03/04 scoping fix (see the "silent data scoping bug" entry) made
`nightly-learn.py` write correctly project-scoped daily snapshot files (`{date}.json` for
kubernaut, `{date}-dcm.json` for dcm) and tagged `mcp-calls.jsonl` entries with `project_dir`.
But `report.py` — the script actually run to view a report — never consumed either of those
fixes. Its multi-day aggregation (`--days N`, the normal mode) reads raw `mcp-calls.jsonl` /
`effectiveness-report.jsonl` / `recall-signals.jsonl` directly, with **no project filtering
anywhere in the file**: `effectiveness-report.jsonl` entries didn't even carry a `project` field
(nightly-learn.py appended kubernaut's and dcm's nightly summaries to the same file, once each,
with no tag distinguishing which was which), `aggregate_mcp_calls` didn't filter by
`project_dir`, and `collect_mental_model_stats()` unconditionally combined both projects' bank
lists. The fix from a week prior only ever addressed the single-night *snapshot files*, not the
*rolling-window report* actually used to check in — the two code paths diverged and only one
got fixed.

**Fix applied**:
- `nightly-learn.py`: `analyze_mcp_effectiveness` now takes a `project` param and writes it into
  the `report` dict appended to `effectiveness-report.jsonl`.
- Backfilled the `project` tag onto the 42 pre-existing entries by cross-referencing each
  entry's `mcp_usage` dict against the corresponding `{date}.json`/`{date}-dcm.json` snapshot
  file's `effectiveness.mcp_usage` (byte-for-byte match, since both are written from the exact
  same in-memory dict) — 39/42 matched exactly; the remaining 3 (2026-06-16/20/22) predate DCM's
  existence as a project entirely and were tagged `kubernaut` directly.
- `report.py`: added a `PROJECT_CONFIGS` dict (kept in sync with `nightly-learn.py`'s), a
  `--project {kubernaut,dcm,all}` flag (default `all`, which now prints both projects as clearly
  separated sections instead of one blended report), and threaded project filtering through
  `mcp_calls` (by `project_dir` prefix), `effectiveness_entries` (by the newly-backfilled
  `project` field, defaulting untagged/pre-DCM entries to kubernaut), `recall_signals` (by bank
  membership), and `analyze_token_consumption`/`collect_mental_model_stats` (by
  `workspace_prefixes`/bank list respectively). Also caught mid-fix: `format_report` was calling
  `collect_mental_model_stats()` unfiltered internally instead of using the already-scoped value
  computed upstream — the mental models table would have silently stayed blended even after
  everything else was fixed.

**Takeaway**: fixing project-scoping at the *write* path (the nightly job) doesn't guarantee the
*read* path (the report script) is fixed too if they don't share the same aggregation code —
they diverged silently for almost a week because nothing exercised `report.py`'s default mode
against two real projects until this request. Worth checking, next time a scoping/multi-tenancy
fix goes into a producer script, whether every consumer of that data was audited too, not just
the one that prompted the original fix.

## 2026-07-08: Prefilter Shadow Trial — No Cheap Gate Safely Narrows Haiku Intake; Found and Fixed a System-Boilerplate Contamination Bug Along the Way

**Context**: Same day as the semantic correction detection spike below, user asked whether
Haiku's intake for "classify every message" (Variant B, the spike's winning design) could be
narrowed with some form of preprocessing, given the embedding gate (Variant A) had already
failed. Proposed running two prefilter candidates in shadow mode against live traffic for a
couple of weeks, scored non-circularly against Haiku's own classifications (not against
`ground_truth.py`, which was itself discovered via keyword search and would make any regex-based
prefilter's recall look artificially good against it).

**What was built**: `spike/prefilters.py` (two candidate gates: `loose_regex_prefilter`, a
deliberately broad recall-oriented regex net distinct from production `CORRECTION_PATTERNS`;
and `trivial_message_exclusion_filter`, a conservative filter that only excludes near-zero-
plausibility messages like bare acknowledgments and bare URLs) and `prefilter-shadow-trial.py`
(a periodic, watermark-based scanner — same incremental-diffing pattern as `nightly-learn.py` —
that calls Haiku on every new top-level user message, logs both prefilters' verdicts alongside
Haiku's real classification to `~/.hindsight/logs/prefilter-shadow.jsonl`, and gates nothing for
real). Backfilled 14 days of existing transcripts for an immediate large sample (mitigating the
risk that live volume would be too low to reach a conclusion during an upcoming 2-week absence),
then installed `launchd/io.vectorize.prefilter-shadow-trial.plist` (`StartInterval`, every 20
minutes, with a PID-based lock file since overlapping unattended runs over 2+ weeks could race on
the watermark file) to keep extending the sample with live traffic.

**Bug found and fixed en route**: The first backfill run showed an implausibly high 15.3%
"correction" rate, and several of the loose-regex-net's "missed" corrections were the *identical*
string repeated dozens of times, e.g. `"The beginning of the above subagent result is already
visible to the user. Perform any follow-up actions (if needed)."` (28 occurrences) and `"Briefly
inform the user about the task result and perform any follow-up actions (if needed)"` (68
occurrences) — these are Cursor's own system-injected Task-tool background-subagent-completion
templates, attributed to `role="user"` in the transcript JSONL despite never being typed by a
human. Haiku sometimes read their instructional phrasing ("perform any follow-up actions") as an
instruction-violation-shaped correction. ~9% of raw "user" messages in the 1-day sample were one
of these (plus a third, `<mcp_server_catalog>...` tool-listing dumps). The existing regex-based
`CORRECTION_PATTERNS` never shared vocabulary with this boilerplate, so **production was
accidentally immune to a false-positive class that a semantic classifier is newly exposed to by
seeing 100% of raw traffic instead of a curated subset.** Fixed in `prefilter-shadow-trial.py`'s
own `extract_user_text` with an explicit boilerplate-prefix/tag exclusion list (also covers
`<system_reminder>`, `<attached_files>`, `<system_notification>`, `<user_info>` defensively, even
though only `<mcp_server_catalog>` appeared in-sample) — not yet ported to `nightly-learn.py` /
`cocoindex-flows.py` since their regex-based detection isn't currently vulnerable to it, but
worth reconsidering if either ever adopts semantic classification.

**Result** (14-day backfill, 3,873 real top-level user messages, 630 Haiku-confirmed
corrections — 16.3% of traffic, itself notably higher than the ~1-2/day assumed from the smaller
hand-curated 7-day scan used to build `ground_truth.py`, which undercounts by construction since
it only sampled messages that already matched a keyword net):

| Candidate prefilter | Haiku-call reduction | Recall vs. Haiku's own verdicts |
|---|---|---|
| Loose regex/keyword net (recall-oriented, broader than `CORRECTION_PATTERNS`) | 90.0% | **24.4%** (154/630) |
| Trivial-message exclusion filter (skips bare acks/URLs only) | 3.4% | 100% (630/630) |

The loose regex net fails even worse than Variant A's embedding gate did in the original spike
(which topped out around 67% recall before collapsing) — it's not just insufficiently tuned, it's
fundamentally the wrong tool: Haiku's notion of "correction" spans far more linguistic variety
(clarifying questions, scope corrections, factual-error callouts) than any keyword list, however
broad, can anticipate. The trivial exclusion filter is safe but nearly worthless — real traffic
essentially never consists of bare acknowledgments or bare URLs, so there's almost nothing safe to
exclude.

**Conclusion**: There is currently no known way to meaningfully and safely narrow Haiku's intake
below "classify everything" (Variant B). Given Variant B's cost is already negligible at this
volume (revised estimate, using the corrected 16.3% correction rate for contradiction-check
volume: still low single-digit dollars/month) and there is no safe cheaper alternative, if
Variant B/contradiction-checking is ever adopted for production, it should run on 100% of
messages with no prefilter gate at all — the earlier idea of "prefilter to reduce intake" is a
reasonable instinct that this evidence now rules out, exactly the kind of negative result the
shadow-trial methodology was built to surface cheaply before any production commitment.

**Takeaways**:
- **The same circularity trap that would have undermined testing embeddings against
  `ground_truth.py` applies to testing any prefilter against it.** Scoring a candidate gate's
  recall against a set that was itself discovered via keyword/regex scanning is close to
  tautological. A live shadow trial scored against a separately-validated classifier's own
  real-time judgments (not a hand-labeled set) is the only way to get a trustworthy, non-circular
  recall number for a prefilter.
- **A near-zero assumed rate should be treated with the same suspicion as the correction-count
  metric that turned out to be a measurement artifact.** The ~1-2/day assumption from the
  hand-curated scan was itself downstream of a keyword search — this is the second time in one
  day that a keyword-discovered sample understated a real rate by an order of magnitude or more.
- **Widening a classifier's input surface from "curated/pre-filtered" to "100% of raw traffic"
  can expose new failure modes the curated set never contained** (here: system-injected
  boilerplate attributed to the wrong role). Any evaluation built on a hand-picked or
  keyword-discovered sample should be treated as necessarily incomplete for this reason, not just
  for coverage of correction *phrasing* but for coverage of message *types*.

## 2026-07-08: Semantic Correction Detection Spike — Embedding Gate Underperforms Regex, Direct LLM Classification Wins

**Context**: Same day as the regex-patching fix below, user asked whether we could do better
than regex entirely: embed transcript messages, find semantic neighbors of known corrections
via a vector DB, validate candidates with Haiku, and separately flag when a new
correction/fact would contradict something already retained in Hindsight. This was scoped
explicitly as a research spike (see
`~/.cursor/plans/semantic_correction_detection_spike_86e447df.plan.md`) — an evidence-backed
"don't adopt" was an accepted outcome, not a failure.

**What was built** (all under `spike/`, nothing wired into production):
- 52-example hand-labeled ground truth (37 corrections across 8 categories — methodology
  violations, convention violations, technical misstatements, undo/revert, repeated mistakes,
  unwanted/unauthorized actions, scope corrections — plus 15 hard negatives, including a
  message where the *user* self-corrects, which is lexically similar to a real correction but
  semantically the opposite), split into a seed subset (33, feeds the vector DB) and a
  held-out eval subset (19, never seen by any pipeline, scores everything).
- `cocoindex.correction_embeddings` pgvector table seeded from the seed split.
- Two candidate-generation variants: **A** (embed message → cosine similarity vs. seed corpus
  → Haiku validates only candidates above a threshold) and **B** (Haiku classifies every
  message directly, no gate).
- A contradiction check (Hindsight `recall()` + LLM judges new-vs-existing) evaluated two
  ways: Config A (Sonnet call) and Config B (13-case synthetic contradiction/non-contradiction
  suite, including two adversarial cases — a "blanket rule vs. narrow exception" case and a
  lexical-overlap-but-unrelated case).
- `contradictions-pending.jsonl` queue + `review-contradictions.py` interactive
  approve/reject/skip CLI + a "Pending Contradictions" line in `report.py`'s nightly report.
- `spike-semantic-correction-detection.py`: the evaluation harness that produced the numbers
  below.

**Result 1 — correction detection: Variant B (classify everything) wins; Variant A
(embedding-gated, the originally proposed design) does not clear the bar at any threshold
tested.** Scored against the 19-example held-out set (15 corrections, 4 benign), never seen
by seeding or few-shot prompts:

| Method | Precision | Recall | F1 | LLM calls | Time |
|---|---|---|---|---|---|
| Regex (production, post this morning's patch) | 1.00 | 0.80 | 0.89 | 0 | ~0s |
| **Variant B (Haiku classifies every message)** | **1.00** | **0.93** | **0.97** | 19 | 12s |
| Variant A, threshold=0.30 (best F1 of the sweep) | 1.00 | 0.67 | 0.80 | 14 | 19s |
| Variant A, threshold=0.35–0.55 | 1.00 | 0.27–0.53 | 0.42–0.70 | 5–11 | 3–10s |

Variant A's F1 *never* beat the already-patched regex baseline at any of 6 thresholds swept
(0.30–0.55), and recall collapses as the threshold rises. Root cause: MiniLM sentence-embedding
similarity between short, stylistically varied corrections and the 33-example seed corpus is
weak and inconsistent — e.g. "why did you remove the sizeLimit?" and "do not use patent search
engine" are genuine corrections that Haiku correctly flags when given the raw text, but score
too low against the seed corpus to ever reach Haiku under Variant A. The embedding gate doesn't
just add complexity (pgvector table, seed corpus maintenance, threshold tuning) — it actively
throws away recall that a direct Haiku call would have caught for free.

At current volume (measured: ~489 user messages/day across both projects), Variant B costs 19
Haiku calls for the entire 19-message eval set in 12 seconds — cost/latency are not a
meaningful constraint at this scale, so Variant A's "cheaper" pitch doesn't offset its recall
loss.

**Result 2 — contradiction check is trustworthy.** Both Sonnet (Config A, the originally
proposed model) and Haiku scored 100% (13/13) on the synthetic suite, including both
adversarial cases, with correct conflicting-memory-index identification on all 7 applicable
cases. Haiku was ~2.4x faster (0.87s vs. 2.12s avg latency) at the same accuracy on this suite
— worth a larger synthetic suite before trusting that parity if this gets adopted, since 13
cases is a small sample for a high-stakes gate. A follow-up real-world sanity check — running
the contradiction check against 6 known-clean confirmed corrections and their actual recall()
results from the live `cursor-memory` bank — surfaced 0 false-positive contradictions.

**Recommendation**: If this gets adopted, use **Variant B (direct Haiku classification, no
embedding gate)** for correction detection — drop the vector-DB design entirely rather than
try to tune it further; the data says the gate is actively harmful here, not just unproven.
For the contradiction check, either model configuration cleared the bar on this suite; Sonnet
remains the more conservative choice for a low-volume/high-stakes gate given the suite's small
size. Adoption itself (wiring into `cocoindex-flows.py`'s live `process_transcript` pipeline)
was explicitly out of scope for this spike and is a separate decision.

**Takeaway**: The originally proposed design (embed → vector DB → gate) is not always the
right shape even when the underlying idea (LLM-validate candidate corrections) is sound —
running both the "clever" and the "obvious" variant side by side against the same held-out
data caught this before any production commitment was made. Worth defaulting to this
side-by-side comparison whenever a spike's design has a "just ask the LLM directly" simpler
alternative available.

## 2026-07-08: Correction Detection Missed 100% of "Not Following Methodology" Corrections

**Context**: User asked whether a specific recurring correction — the model mistaking
TDD REFACTOR-phase work for a CHECKPOINT gate (or vice versa) in `kubernaut`'s
RED/GREEN/REFACTOR + CHECKPOINT A/B/C/D/DD/W workflow (see `kubernaut/AGENTS.md`) —
was being captured by the effectiveness pipeline, given they'd corrected it "plenty"
over the prior two days. `corrections_detected` had read `0` for both projects for
three days straight (2026-07-06 through 2026-07-08), which was itself a red flag
given the user's report of frequent live corrections.

**Root cause**: `CORRECTION_PATTERNS` (duplicated in `nightly-learn.py`, `report.py`,
and `cocoindex-flows.py`) is a fixed list of ~10 regexes for generic corrective
phrasing ("no, that's wrong", "don't do that", "undo that", etc.). None of them
match this user's actual, highly consistent phrasing for methodology/convention
corrections. Scanned the last 7 days of top-level transcripts (subagents excluded)
for correction-adjacent language and hand-verified each hit: **16 genuine
corrections, 0 detected** by the existing patterns. Examples that were silently
invisible to every downstream metric (`corrections_detected`, `recall_session_stats`,
and — most importantly — the `[CORRECTION]`-tagging in `cocoindex-flows.py` that
feeds the `cursor-memory` Hindsight bank):

- "again, you're not following AGENTS.md"
- "no, you're still not following the project's methodology"
- "you keep making the same mistake with refactor phase: you're not aligned with..."
- "why does REFACTOR still show checkpoint tasks? it should be split. You're still
  not following the AGENTS.md"
- "these tests are not following project convention https://..."
- "I'm finding often that the model tends to mistake TDD refactoring for checkpoint"

The existing "no, that's wrong"-style pattern requires the literal word "that's";
none of the above use it, despite being unambiguous corrections to a human reader.

**Fix applied**: Added four new patterns to all three `CORRECTION_PATTERNS` copies:
`you're/you are (still) not following|aligned`, `not following the
methodology/convention/AGENTS.md/CLAUDE.md`, `you keep making the same mistake`, and
`mistak(e|ing) ... for ...` (catches "mistake X for Y" conflation reports like the
TDD/checkpoint one above). Verified against the full 7-day sample plus a battery of
adversarial near-misses ("confidence score... by mistake", "I'm still not clear on
1578", "what should be? I'm confused") to confirm no false positives — result: 11/11
genuine corrections now caught (the remaining 5 unmatched hits were correctly
filtered as non-corrections), zero regressions on the benign set.

**Not yet done**: This only fixes *detection going forward* (tonight's nightly run
onward). It does not retroactively backfill `corrections_detected` counts for past
days the way `backfill-effectiveness.py` did for recall-adoption — the raw signal
(transcript text) is still on disk, so a similar backfill is possible if the
historical trend line becomes valuable, but wasn't done here since correction
counts aren't currently plotted in `weekly_trend`.

**Takeaways**:
- **A near-zero rate on a metric that should clearly be nonzero is itself a signal
  worth investigating before trusting the number.** `corrections_detected: 0` for
  three consecutive days, next to a user explicitly saying they corrected the model
  "plenty", should have been the tell — the absence of data was the bug report.
- **Regex-based intent detection silently rots as phrasing drifts.** This user's
  actual correction style ("you're not following X", "you keep making the same
  mistake") is completely different from the patterns the list was originally
  seeded with ("no, that's wrong"). Worth periodically re-deriving patterns from a
  sample of real recent corrections rather than trusting a static list indefinitely.
- **This pattern list has three independent copies** (`nightly-learn.py`,
  `report.py`, `cocoindex-flows.py`) that must be kept in sync by hand — the
  `cocoindex-flows.py` copy is the most consequential of the three since it's what
  actually tags `[CORRECTION]` windows for ingestion into the `cursor-memory`
  Hindsight bank; a fix applied only to the reporting copies would still leave the
  memory system blind to this class of correction. Worth extracting to a shared
  module if a fourth copy is ever needed.

## 2026-07-07: Data Freshness Alarm Was Unmeasurable, Not Stale — Plus a Real Upstream Fix

**Context**: `report.py`'s "Data Freshness" section had been flagging Docs/Code/
Transcripts as several hours "STALE" every morning (target ≤1hr) since at
least 2026-07-04. Investigated whether this was a real ingestion problem or
another measurement artifact, and separately looked into why Cursor shows the
Hindsight MCP servers as down most mornings.

**Root cause (freshness)**: `collect_freshness_stats()` derived staleness from
the last `"docs-app"`/`"code-app"`/`"transcript"` log line matching
"watching"/"complete"/"file-watching" in `cocoindex-stderr.log`. Checked what
actually emits those lines: CocoIndex's live file-watcher apps only log
`"Starting <app> (live, file-watching)..."` **once at process startup** —
there's no periodic "still watching" or per-file "indexed X" line, and the
underlying `cocoindex.code_embeddings` table has no `updated_at` column
either (confirmed via direct schema inspection). So the metric was measuring
"time since the watcher process last restarted", not "time since data was
actually indexed" — a perfectly healthy, idle watcher with no local file
changes is indistinguishable from a dead one by this signal alone. Compounding
this: `io.vectorize.hindsight.restart.plist` kills `cocoindex-flows` (in
addition to `hindsight-api`) every night at 1am, so the "staleness" clock
reset nightly regardless of real indexing activity — explaining why it never
read below ~4-10 hours each morning.

**Root cause (why cocoindex was being killed nightly in the first place)**:
Traced this back to a known upstream bug: on macOS, FSEvents can silently
stop delivering file-change notifications after long-running watch sessions,
and the old `cocoindex` live-watcher had no recovery path — it blocked
indefinitely on the event queue. The nightly kill-and-respawn was almost
certainly a workaround for this (undocumented, predates this project). Checked
upstream: `cocoindex-io/cocoindex#2232` ("add periodic rescan + watcher
recreation for live mode") fixes exactly this with a `rescan_interval`
(default 1hr) that periodically tears down and recreates the watcher, no
restart needed — **we authored and submitted this PR** (during earlier work
on this project), it was merged upstream 2026-06-30, and shipped in PyPI
`cocoindex` 1.0.15 (2026-07-04) and 1.0.16 (2026-07-06). We were still pinned
to 1.0.11 (2026-06-17), predating both our own fix and its release — i.e. we'd
fixed the root cause upstream 8 days earlier and just hadn't pulled it in.

Separately checked the other upstream contribution from this project,
`vectorize-io/hindsight#2529` (the `DeadlockDetectedError` retry fix, also
authored by us — see 2026-07-02 entry) and its maintainer follow-up `#2534`
— **both still open, unreviewed, unmerged** as of this writing. No new
`hindsight-api` release contains either fix yet.

> **Follow-up needed**: periodically check `gh pr view 2529 --repo
> vectorize-io/hindsight` (and `2534`) for merge status. Once either merges
> and a new `hindsight-api` PyPI release includes it, upgrade the same way
> `cocoindex` was upgraded here (`uv pip install --python
> ~/.hindsight/venv/bin/python -U 'hindsight-api[all]'`) and confirm the
> deadlock stops appearing in `hindsight-stderr.log`.

**Fixes applied**:
1. Upgraded `cocoindex` 1.0.11 → 1.0.16 in `~/.hindsight/venv` (`uv pip
   install -U cocoindex`) and restarted the service — now self-heals FSEvents
   staleness on its own every hour, no process restart required.
2. Removed `pkill -f cocoindex-flows` from `io.vectorize.hindsight.restart.
   plist` (kept the `hindsight-api` restart) — it was a workaround for a bug
   that's now fixed upstream and had no other known purpose. Reversible via
   git history if cocoindex misbehaves without it.
3. Reworked `collect_freshness_stats()`/the report's Data Freshness section to
   stop presenting a fabricated Healthy/STALE verdict for docs/code/
   transcripts. They now show "watcher uptime" as informational only; only
   "issues" (which has a genuine ~300s periodic poll signal) gets a real
   pass/fail verdict.

Versions as of this entry, for future incident triage: `cocoindex` 1.0.16,
`hindsight-api` 0.8.4 (last upgraded 2026-07-03, see that entry — unrelated to
and unaffected by the still-open deadlock PRs above).

**On "Cursor shows the MCP as down every morning" (not fully solved)**:
`hindsight`/`hindsight-docs`/`hindsight-issues` are configured as `type:
"http"` MCP servers pointing at `localhost:8888` — a connection Cursor holds
open, unlike the `stdio`-transport `cocoindex-code`/`gopls` servers Cursor
spawns fresh per use. When the 1am `pkill -f hindsight-api` drops that
connection, Cursor's HTTP MCP client does not appear to automatically retry
in the background; the server shows red until a manual reload (of the MCP
panel or the whole window). This is Cursor client-side reconnection behavior,
not something fixable from this repo. Removing the `cocoindex-flows` kill
(fix #2 above) narrows the nightly disruption window to `hindsight-api` only,
but doesn't eliminate it — `hindsight-api` still restarts nightly and its
original justification predates this project and was never documented (see
2026-06-26 entry). Left as-is pending more evidence on whether that restart
is still needed at all.

**Takeaways**:
- **A log-line-based "last activity" signal is only as good as how often that
  line actually fires.** A one-time-at-startup log line makes a terrible
  proxy for "still healthy" — it can only ever measure uptime, never real
  activity, no matter how you interpret the number.
- **When a workaround (nightly kill-and-respawn) has no documented reason,
  check upstream before assuming it's still needed.** In this case the
  workaround's likely root cause had already been fixed by an accepted PR
  sitting in a newer release we simply hadn't pulled — the fix was to update
  a dependency, not to keep re-applying the workaround.
- **HTTP-transport local MCP servers are more fragile to backend restarts
  than stdio-transport ones**, because Cursor holds a live connection to the
  former but spawns the latter fresh per invocation. Worth factoring into
  future MCP server design decisions for anything that needs to restart
  periodically.

---

## 2026-07-04: "40% Recall Adoption" Was a Measurement Artifact, Not a Rule Failure

**Context**: `report.py` flagged recall adoption at ~40% ("agent is not recalling
in most sessions"), pointing at the `alwaysApply` rule in
`.cursor/rules/hindsight-memory.mdc` as possibly unreliable. Investigated by
recomputing `analyze_mcp_effectiveness` over a true, deduplicated 7-day window
(264 transcripts) instead of report.py's summed daily snapshots, then splitting
the result by transcript type.

**Root cause**: `find_recent_transcripts` globs `agent-transcripts/**/*.jsonl`,
which recursively matches both top-level conversation transcripts *and*
`.../subagents/<id>.jsonl` transcripts created by the `Task` tool. Of the 264
transcripts in the window, 207 (78%) were subagents, not user-facing
conversations. Splitting recall adoption by type:
- Top-level conversations: 45/55 = **81.8%** recall adoption — healthy, the
  rule is working as intended.
- Subagent transcripts: 46/199 = **23.1%** — and of the 153 subagent sessions
  without recall, 152 made *zero* MCP tool calls of any kind in the entire
  transcript (only 1 had MCP access but chose not to recall). This means
  recall wasn't skipped — it was **structurally unavailable**, most likely
  because these were `explore`/readonly subagents, which per the `Task` tool's
  own contract run with "no MCP or internet access."

Blending both populations into one "sessions_with_recall / sessions_without_
recall" ratio produced a number that looked alarming but was mostly measuring
"what fraction of transcripts happened to be read-only research subagents,"
not "is the agent ignoring the memory rule."

**Fix**: `analyze_mcp_effectiveness` in `nightly-learn.py` now skips any
transcript path containing `/subagents/` before per-session recall scoring,
and reports the excluded count as `subagent_sessions_excluded` for
transparency. `report.py` surfaces that count next to the adoption line. No
change was made to the `hindsight-memory.mdc` rule — it wasn't the problem.

Rather than wait ~7 days for the rolling window to fill back up with
corrected snapshots, added `end_time`/`report_date` override parameters to
`find_recent_transcripts`/`analyze_mcp_effectiveness` (both default to
now/today, so live nightly runs are unaffected) and a new
`backfill-effectiveness.py` that replays each historical night's exact 24h
window — reconstructed from the existing daily JSON's own mtime — against
transcripts and `mcp-calls.jsonl` still on disk. This retroactively corrected
2026-06-27 through 2026-07-03 in place (only the outer `effectiveness` key of
each daily JSON; everything else untouched) and, as a side effect, also fixed
those same days' pre-existing "identical effectiveness/mcp_usage across
kubernaut and dcm" bug (the 07-03 fix that added `workspace_prefixes` scoping
also only applied going forward until this backfill). `report.py --days 7`
went from 40.6% to 79.9% recall adoption immediately after backfilling,
instead of six more days of degraded/misleading dashboard data.

**Takeaways**:
- **A metric that blends two structurally different populations (user-facing
  sessions vs. delegated, often tool-restricted subagent runs) will trend
  toward whichever population is more numerous** — here subagents outnumbered
  real conversations ~4:1, so their near-zero MCP access dominated the signal.
- **Before treating a low-adoption metric as a rule-compliance problem, check
  whether the tool being measured was even *available* in the sessions being
  counted.** A session with zero MCP calls of any kind (not just zero recall
  calls) is a strong signal of "couldn't," not "didn't."
- When adding new session-derived metrics, decide explicitly whether subagent
  transcripts belong in the denominator, and if so, track them as a distinct
  bucket rather than merging them silently into "sessions."
- **Derived metrics computed from durable raw sources (transcripts,
  append-only logs) are backfillable, not just fixable-going-forward** — as
  long as the scoring function takes an explicit time window instead of
  hardcoding `datetime.now()`, a bug fix can be replayed against historical
  windows instead of waiting for the rolling window to refill. Worth
  designing new analytics this way from the start (explicit `end_time` param)
  rather than retrofitting it under pressure, as done here.

---

## 2026-07-04: PEP 604 Union Syntax Silently Broke the Nightly Pipeline

**Context**: The effectiveness-scoping fix from 2026-07-03 (`workspace_prefixes:
list[str] | None = None`) crashed every `nightly-learn.py` invocation — hourly
and nightly, both projects — starting the moment it was deployed. No corrections
were retained, no reflect/probes/triage ran, and no `2026-07-04.json` /
`2026-07-04-dcm.json` report was generated overnight, discovered only when
asked for a status report the next morning.

**Root cause**: launchd invokes `nightly-learn.py` via `/usr/bin/python3` —
macOS's bundled system Python, pinned at 3.9.6 — not the project's `~/.hindsight/
venv` (3.14). Python evaluates function annotations eagerly at import time
unless told otherwise, and PEP 604's `X | Y` union syntax (`list[str] | None`)
isn't valid until 3.10. The failure was a plain `TypeError` at module load,
so *every* run failed identically and immediately — but nothing surfaced it in
real time (no alerting on launchd job failures), so 18+ hourly runs and both
nightly runs failed silently overnight before anyone asked for a status.
`report.py` had the same latent issue (`dict | None` at line 544), pre-existing
and unrelated to the 07-03 change — it just isn't scheduled, so it never
crash-looped, only would fail if manually run under system Python.

**Fix**: Added `from __future__ import annotations` to both scripts, deferring
annotation evaluation to strings. Verified neither script does runtime
introspection on annotations (no pydantic/dataclass/`get_type_hints`), so this
is a pure compatibility fix with no behavior change. Manually re-ran both
nightly jobs afterward to backfill the missed 2026-07-04 reports.

**Takeaways**:
- **Any code invoked via `/usr/bin/python3` in a launchd plist must target
  Python 3.9 syntax**, not whatever version is used for local testing/dev.
  `from __future__ import annotations` at the top of every launchd-invoked
  script is cheap insurance against this entire class of bug.
- **Test changes against the actual invocation path, not just `python3` in a
  dev shell.** `python3 -c "import nightly_learn"` under the venv's 3.14 would
  never have caught this; only running it exactly as launchd does
  (`/usr/bin/python3 nightly-learn.py`) surfaces it.
- **A crashing scheduled job produces no report and no error visible to the
  user** — it just silently doesn't happen. There's currently no alerting for
  "the nightly job didn't run" (as opposed to "the nightly job ran and
  reported errors"), which is a gap worth closing given this is the second
  silent-failure incident in two days.

---

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
