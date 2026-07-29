# Research Findings

Historical record of empirical findings from running Engram in production.

## 2026-07-29: Comparative Analysis Against tstockham96/engram — 8 Enhancement Opportunities Filed

**Trigger**: User asked for a comparison between this project and
[tstockham96/engram](https://github.com/tstockham96/engram) — an unrelated, same-named, independently
developed npm package ("Universal memory layer for AI agents... knowledge graphs, consolidation, and
spreading activation") — to identify overlaps and enhancement opportunities. No prior relationship
between the two projects; the name collision is coincidental.

**Method**: Read this repo's own architecture docs (`README.md`, `docs/README.md`) plus a shallow
clone of `tstockham96/engram` (`README.md`, `package.json`, and source: `types.ts`, `temporal.ts`,
`extract.ts`, `store.ts`, `vault.ts`, `embeddings.ts`, `brief.ts`, `claude-watcher.ts`). Cross-checked
proposed findings against this repo's actual code (`contradiction_resolution.py`) before writing them
up, to avoid recommending something already implemented (confirmed, e.g., that local contradiction
resolution runs at retain-time via Sonnet `check_contradiction()` but **hard-deletes** the superseded
document in live mode — see issue #1 below — rather than marking it superseded).

**Conclusion — same problem, different architectures**: both projects tackle "give AI agents memory
that survives across sessions," but jordigilh/engram is a Python orchestration layer around an
external Hindsight (Postgres/pgvector) service plus CocoIndex ingestion, macOS-only, nightly-batch LLM
cost model (Vertex AI Haiku/Sonnet, ~$0.12/night), multi-bank + tag-isolated, with hybrid
tree-sitter/BM25/dense code search and GitHub issue/PR ingestion that tstockham96/engram has no
equivalent of. tstockham96/engram is a self-contained TypeScript/SQLite npm package (`engram-sdk`),
cross-platform, per-call LLM cost model (pluggable Gemini/OpenAI/Anthropic), single shared vault
across all projects/agents, with several mechanisms this repo lacks: true bi-temporal versioning
(`validFrom`/`validUntil` + point-in-time `asOf` queries), write-time contradiction detection blended
into every `recall()` via spreading activation (tunable hops/decay/entity-hops), a synthesized `ask()`
tool with graded confidence/evidence-quality, a docs-vs-vault `audit()` tool, on-demand `checkpoint()`
retain (not nightly-only), a no-LLM regex fallback extractor, first-class `pending`/`fulfilled`
commitment tracking with an `alerts()` aggregation query, and a published external benchmark score
(LOCOMO: 80.0% vs. Mem0's 66.9%) instead of purely self-referential trend metrics.

**Overlaps confirmed**: both frame flat markdown memory (CLAUDE.md-style) as the baseline to beat;
both do episodic-facts → distilled-knowledge consolidation (retain/reflect vs.
episodic/consolidate/semantic); both do LLM-verified contradiction detection with
auto-resolve-above-threshold / queue-below-threshold semantics; both build an entity/knowledge graph;
both passively ingest agent transcripts; both inject context at session start; both self-measure
effectiveness (weekly trend dashboard vs. LOCOMO benchmark); both are MIT-licensed, local-first, no
telemetry.

**Outcome**: 8 enhancement opportunities filed as GitHub issues (this repo previously had zero —
first issues created), each with full context, source-repo reference implementation, proposed
approach, expected impact, risks, and acceptance criteria so they're implementable later without
re-deriving this analysis:

| # | Title | Notes |
|---|-------|-------|
| [#1](https://github.com/jordigilh/engram/issues/1) | Preserve supersession history instead of hard-deleting contradicted memories (bi-temporal model) | Highest priority — directly de-risks the existing shadow→live auto-resolve rollout already tracked in the 2026-07-12/07-26 entries below |
| [#2](https://github.com/jordigilh/engram/issues/2) | Add synthesized `ask()` tool with confidence + evidence-quality grading | Additive; must stay opt-in to preserve the zero-LLM-cost recall guarantee |
| [#3](https://github.com/jordigilh/engram/issues/3) | Add docs-drift `audit` tool cross-checking `docs/*.md` against current mental models/facts | Same category of problem `check-rule-sync.py` already solves for code, applied to prose |
| [#4](https://github.com/jordigilh/engram/issues/4) | Add on-demand same-day retain path for corrections (checkpoint-style, not nightly-only) | Reuses ~100% of existing `nightly-learn.py` extraction logic with a narrower scope |
| [#5](https://github.com/jordigilh/engram/issues/5) | Add no-LLM fallback extraction for retain resilience when Vertex AI is unavailable | Closes the gap between "recoverable after total loss" and "resilient to a transient outage" |
| [#6](https://github.com/jordigilh/engram/issues/6) | Investigate exposing tunable entity-graph spreading/expansion parameters on recall | Gated on what the external Hindsight API actually supports — may resolve to an upstream feature request |
| [#7](https://github.com/jordigilh/engram/issues/7) | Add first-class commitment/pending-item tracking + proactive alerts query | Distinct failure mode from corrections — dropped follow-ups aren't measured today |
| [#8](https://github.com/jordigilh/engram/issues/8) | Evaluate adopting an external memory-recall benchmark (e.g. LOCOMO) for cross-system comparability | Research spike first — LOCOMO targets general conversational memory, fit for a coding-assistant pipeline is unconfirmed |

**Follow-up**: issues #1 and #7 both depend on the same underlying discovery task (whether Hindsight's
memory schema supports a non-`active` status transition beyond hard delete) — resolve that
investigation once and apply to both rather than duplicating it.

**Prioritization (same day)**: user asked for the 8 issues to be ranked by impact and complexity.
Built a scored matrix (Impact×2 − Complexity, each on a 1–3 scale) as a canvas
(`enhancement-issues-prioritization.canvas.tsx`) rather than a chat table, since it's a standalone
analytical artifact. Result: Quick Wins (ship first, no dependencies) = #4, #5; Big Bets (high impact,
real complexity, share the #1/#7 discovery-step dependency above) = #1, #7; Nice-to-Have (schedule
when capacity allows) = #2, #3; Spike First (validate feasibility before committing further work) =
#6, #8.

## 2026-07-29 (same day, follow-up): Filed #9 — Linux/Fedora Support, a Gap Identified But Not Filed in the Original Batch

**Trigger**: User requested a 9th issue for Linux/Fedora support after the original 8 were filed and
prioritized. This traces back to the "Portability / cross-platform" item flagged in the very first
comparison response earlier the same day but deliberately left out of the initial batch of 8 (it
wasn't inspired by a mechanism *in* tstockham96/engram the way #1–#8 were — tstockham96/engram is
cross-platform by construction, so there was nothing to port from it; it's a standalone gap in this
repo, surfaced by contrast rather than by example).

**Diagnosis — confirmed macOS coupling is deeper than "just launchd"**: before filing, verified the
actual scope rather than assuming. Found: (1) all 9 `launchd/*.plist` files have no systemd
equivalent; (2) `docs/INSTALL.md`'s Prerequisites section opens with "macOS (tested on Mac Studio M2
Max...)" and assumes Homebrew throughout; (3) Hindsight's embedded Postgres (`pg0`) and `MPS/ONNX`
embedding backend have never been run on Linux — Linux viability is genuinely unverified, not just
undocumented; (4) **this project used to run in a container and deliberately migrated away** —
`migrate-to-native.sh` is a still-present one-shot script that exports/re-imports all memory banks
from a **Podman**-run Hindsight container to the native macOS install, ending in `podman stop
hindsight`. The *reason* for that migration (likely embedding performance or avoiding container
overhead, given the MPS-acceleration architecture that followed) was never written down in this file
— a gap now called out explicitly in issue #9's risk section so it doesn't get rediscovered the hard
way a second time before anyone reintroduces a container-based Linux path.

**Notable finding**: a `Dockerfile` still exists in the repo (undocumented, unused by
`docs/INSTALL.md`), layering `google-cloud-aiplatform` onto a maintained upstream
`ghcr.io/vectorize-io/hindsight:latest` image. Since Fedora ships Podman by default and this
project's prior container deployment already used Podman, reviving that path — rather than building
systemd parity for all 9 launchd plists from scratch — may be the cheaper route specifically for
Fedora. Filed as two explicit candidate paths (native systemd parity vs. reviving the container path)
with a feasibility spike as the required first step for either.

**Outcome**: [#9](https://github.com/jordigilh/engram/issues/9) filed with the same
Summary/Source/Current-Behavior/Proposed-Enhancement/Risks/Acceptance-Criteria structure as #1–#8.
Rated Impact=Medium (real, user-requested; mitigates the single-host-availability problem already
noted in the 2026-07-28 entry below), Complexity=High (touches nearly every operational script, two
unverified platform-support assumptions), landing in the same "Big Bet" tier as #1/#7 in the
prioritization canvas (score 1 — schedule after the discovery-step work, not blocking, not urgent).
The canvas and this table are both updated to include it.

## 2026-07-29 (same day, second follow-up): Resolved #9's Open Design Question — Why Native Won on macOS, and Why Container Is Right on Linux

**Trigger**: user supplied the previously-missing reason behind `migrate-to-native.sh`'s
container→native migration, closing the exact gap issue #9's risk section had flagged ("this
should be resolved as a documentation gap regardless of which path is chosen").

**The explanation**: Podman on macOS does not run containers on the host kernel at all — macOS
(XNU) has no Linux namespaces/cgroups, so Podman provisions a Linux VM (`podman machine`, via
QEMU/AppleHV) and every container actually runs inside that VM. That VM is a managed, occasionally
recreated entity — upgrades, corruption recovery, and resource reconfiguration commonly involve
`podman machine rm` + `podman machine init` — so a long-lived stateful service like Hindsight's
embedded Postgres data risks being tied to that VM's lifecycle rather than surviving independently
of it, unless every byte lives on a carefully-maintained host bind-mount. That's the real reason
native process won on macOS: it has no VM layer, hence no intermediate lifecycle to accidentally
reset.

On Linux, this risk **does not exist**: Podman/Docker run containers directly on the host kernel, so
a bind-mounted volume is just a host directory — there's no separate VM disk to lose independently
of the host. The same tradeoff that favored native on macOS therefore favors the container path on
Linux, not merely tolerates it.

**A second, previously-unstated consequence**: this also substantially de-risks the "does
`pg0`/ONNX even run on Linux" question that issue #9 originally treated as unverified and requiring
its own feasibility spike. Container images are Linux userspace regardless of host OS — so if the
`Dockerfile`-built image ran successfully under Podman-on-macOS (i.e., inside that Linux VM) before
the 2026-07 native migration, its `pg0`/ONNX backend was already running on Linux the whole time.
The remaining unknown narrows from "does this work on Linux at all" to "does the *current*
`Dockerfile` still build and run against today's `hindsight-api`" — a currency check, not a
viability spike.

**Resolution — platform-conditional architecture, not a global A-vs-B choice**: macOS stays native
(unchanged, no regression risk); Linux/Fedora uses the existing (currently dormant) `Dockerfile` as
its target deployment, with Podman **Quadlets** (`.container` unit files, auto-generated into
systemd services) as the idiomatic way to run it as a managed service on Fedora specifically.
Whether the CocoIndex/nightly-learn Python scripts should also be containerized on Linux was left as
an explicit open scoping question — the VM-recreation risk that justifies containerizing the
*stateful* Hindsight service doesn't obviously extend to those mostly-stateless batch/poll jobs, so
the simpler default is native Python + systemd timers for those, revisited only if a concrete reason
to containerize them shows up.

**Outcome**: issue #9's body rewritten to reflect the resolved architecture (no longer presented as
an open "Path A vs. Path B" decision), with a comment added recording the rationale verbatim for
traceability independent of this file. Complexity re-rated Medium (down from High) in the
prioritization canvas — the biggest source of uncertainty is gone — moving its score from +1 to +2,
tied with #2/#7 while remaining in the "Big Bet" tier given the real implementation work still ahead
(Quadlet units, systemd timers, a new install doc).

## 2026-07-29 (same day, third follow-up): Phase 1 of the Enhancement Backlog — #4 and #5 Implemented, Both Premises Corrected During Planning

**Trigger**: user asked to plan and implement "Phase 1" of the prioritization canvas's Quick Wins
tier (#4, #5) — the two issues rated no dependencies, ship first. Full TDD (RED→GREEN→REFACTOR) per
issue, as two separate commits.

**Both issues' original write-ups turned out to rest on a stale or incorrect premise** — caught by
re-reading the actual current code/config before implementing, not by the user:

- **#4** assumed corrections wait for the next 2 AM nightly run (next-day latency). Stale:
  `launchd/io.vectorize.hindsight.hourly.plist` already runs `nightly-learn.py --mode hourly` every
  hour against a rolling 2h window, so corrections are already retained within ~1-2h today, not
  overnight — `docs/README.md`'s "Retain in nightly batch" Key Design Decisions row predates that
  hourly job and was stale too. Re-framed the real remaining gap as **determinism + session-scoping**
  (force-retain *this* transcript right now, e.g. before a context-compaction event or before ending
  a session with uncommitted corrections) rather than latency.
- **#5** assumed a local, client-side Haiku call in `nightly-learn.py`'s retain path that could be
  wrapped with a fallback. Incorrect: hindsight-api runs its own structured-pattern extraction
  server-side (configured by `HINDSIGHT_API_RETAIN_LLM_MODEL`) — there is no local LLM call to wrap.
  What *is* local and real: `retain_windows()`'s bare `except Exception` silently dropped a window
  whenever `api_post()` failed, with zero recovery path. Re-framed #5 as a **local recovery buffer**:
  narrow the catch to `HTTPError`/`URLError`/`TimeoutError` specifically (a genuine transient-outage
  signature, not "any exception"), run a cheap offline heuristic extraction on the window instead of
  losing it, and buffer it for replay once hindsight-api recovers.

Both issue bodies and a corresponding GitHub comment were updated in-place to record the correction
before implementation began, so the historical "why" isn't lost the way the pre-hourly-job
`docs/README.md` row nearly was.

**#4 outcome**: [`retain-now.py`](https://github.com/jordigilh/engram/issues/4) — a thin CLI reusing
100% of `nightly-learn.py`'s existing `extract_learning_windows()`/`retain_windows_deduped()`
pipeline against exactly one resolved transcript path, updating watermarks/hashes afterward so the
next hourly/nightly run doesn't double-process it. `docs/README.md` gained an "On-Demand Retain"
subsection. Commit `c164962`.

**#5 outcome**: [`fallback_extract.py`](https://github.com/jordigilh/engram/issues/5) — regex-based
entity (CamelCase/acronym/Capitalized-Phrase)/topic(repo-vocabulary)/salience heuristic extraction,
mirroring `tstockham96/engram`'s `src/extract.ts` in spirit but tuned to this repo's own domain
vocabulary; `record_fallback()`/`load_backlog()`/`save_backlog()` manage
`~/.hindsight/logs/fallback-retained.jsonl` with the same tmp-then-rename atomicity as
`nightly-learn.py`'s own `save_watermarks()`. `nightly-learn.py` gained
`reprocess_fallback_backlog()` and `--mode reprocess-fallback` to retry buffered entries once
Vertex AI/hindsight-api recovers. `report.py` gained `count_fallback_backlog()` and a
`FALLBACK-EXTRACTED` report section so a growing backlog is a visible signal, not a silent one.
`docs/METRICS.md` gained a "Fallback Extraction Backlog" section. Commit `fd1b570`.

**Both issues left open** (not closed) pending the user's own verification/merge decision, consistent
with how this repo has handled issue-to-implementation so far — this file and the commits are the
source of truth for what shipped, not issue state.

## 2026-07-28: Low W30/W31 Session Volume Explained by PTO + Frequent Host Shutdowns, Not Reduced Engagement or a Regression

**Context**: While reviewing whether Engram is reducing tokens/corrections, the weekly trend showed
small and volatile sample sizes (6, 41, 7 sessions across W29-W31) alongside a corrections/session
figure that couldn't be cleanly attributed to real behavior change vs. the concurrent regex→Haiku and
Haiku v1→v2 correction-detection fixes (see 2026-07-09 and 2026-07-27 entries below). User clarified:
the past two weeks were largely PTO, and the host running Hindsight/CocoIndex was shut down often
during that period — both directly reduce the number of real coding sessions available to measure,
independent of anything about Engram's effectiveness.

**Implication for reading the trend**: the low/volatile weekly session counts in this window are an
**activity-volume artifact** (fewer sessions existed to sample from), layered on top of the already-
known **detection-methodology artifact** (what counts as "a correction" changed twice mid-window).
Neither the corrections/session trend nor the productivity-density trend for W30-W31 should be read as
a signal about whether Engram got better or worse over this period — there isn't yet a clean window of
normal usage under the current (v2) classifier to draw that conclusion from.

**Follow-up**: re-check the corrections/session and productivity-density trend once ~2 weeks of normal
(non-PTO, host-up) usage have accumulated under the v2 `classify_correction` prompt, rather than by
calendar weeks alone.

## 2026-07-27: `cursor-memory`'s Shared Mental Models Were ~100% Kubernaut-Specific — DCM Recall Polluted With FedRAMP Content

**Trigger**: A DCM session reported "The hindsight recall returned unrelated project conventions
(kubernaut/FedRAMP) — not applicable here." This is distinct from the 2026-07-13
`ALLOWED_WORKSPACE_PREFIXES` fix (which stopped *out-of-scope* workspaces like `koku` from feeding
`cursor-memory`) — this pollution was coming from **inside** the three onboarded projects
(kubernaut/dcm/engram) themselves, which `cursor-memory` is intentionally shared across for
universal coding-hygiene lessons.

**Diagnosis**: Pulled the actual content of `cursor-memory`'s 4 core auto-refreshing mental models
(`testing-methodology`, `coding-conventions`, `architecture-decisions`, `workflow-preferences`) —
all 4 had **zero tags** and, on inspection, `testing-methodology`'s ~4,700-word synthesized document
was almost entirely kubernaut/Go-specific: Ginkgo/Gomega exclusively, `go build`/`golangci-lint`,
CRD regeneration, *100 Go Mistakes* checklist, and — the reported issue — FedRAMP/NIST-800-53/SOC2
control-mapping requirements baked into 4+ separate sections (mandatory plan sections, GA readiness
audit dimensions, confidence thresholds, formal test plan template). None of that applies to DCM
(Go, but no FedRAMP mandate) or Engram (Python/pytest). A `recall()` probe with the exact
mandatory-gate query ("project methodology, workflow, conventions") independently confirmed the
same thing at the raw-fact level: of ~50 results, the large majority were kubernaut-specific
(FedRAMP/NIST-800-53/SOC2 control mapping, Ginkgo/BDD test IDs, workflow-selection CRD design), all
untagged.

Root cause, two layers, both stemming from the same gap — **nothing in the retain pipeline ever
tagged a fact with which project it came from**:
1. `nightly-learn.py`'s `retain_windows()` receives a `project` parameter (kubernaut/dcm/engram,
   forwarded from `project_for_transcript_path()` since the 2026-07-19 contradiction-queue fix) but
   only used it to pass to `contradiction_resolution.resolve()` — it was never written onto the
   retained item's own `tags` field. Every fact in `cursor-memory`, from any project, has always
   been untagged.
2. The 4 mental models' refresh `trigger` had no tag-based input filtering either
   (`tag_groups: null`), so even a hypothetically-tagged fact set wouldn't have changed which facts
   fed the reflect() call that builds these documents.

**Constraint discovered while designing the fix**: existing facts can never be retagged
retroactively. Checked both the MCP `update_memory` tool and the underlying REST
`PATCH /v1/default/banks/{bank_id}/memories/{memory_id}` endpoint directly (`UpdateMemoryRequest`
schema) — neither supports a `tags` field at all; only `text`/`context`/`occurred_start`/
`occurred_end`/`fact_type`/`entities`/`state` (invalidate/revert) can be edited post-hoc. Tags can
only be set once, at `retain` time. This means the ~19+ (likely more) already-retained
kubernaut/FedRAMP-specific raw facts identified during triage can never be tagged or excluded from
recall after the fact — only new pollution can be stopped, not the existing backlog.

**Fix** (four parts, confirmed against the live `hindsight-api` at `localhost:8888`):
1. **`nightly-learn.py`**: `retain_windows()` now writes `tags=[project]` (plus any existing
   `CORRECTION`/`supersedes-prior-memory` tags) onto every retained `cursor-memory` item when a
   project is known. Regression tests added (`TestRetainWindowsProjectTagging`, 4 cases covering
   plain instructions, no-project backward compat, combination with the auto-resolved
   `supersedes-prior-memory` tag, and corrections with no contradiction). 218/218 tests pass.
2. **Tagged the 4 existing mental models** `["kubernaut"]` via direct `PATCH
   /v1/default/banks/cursor-memory/mental-models/{id}` (the MCP `update_mental_model` tool doesn't
   expose the full `trigger` object, only `name`/`source_query`/`max_tokens`/`tags`/
   `trigger_refresh_after_consolidation` — needed the REST API directly to also set
   `trigger.tags_match: "any"` on each, preserving their existing `mode`/`refresh_after_consolidation`
   settings). `tags_match: "any"` is required here specifically *because* all the rich pre-fix
   content is untagged — `MentalModelTrigger`'s documented default of `all_strict` when a model has
   tags ("security isolation") would have emptied out 6+ weeks of legitimate kubernaut history the
   moment the `["kubernaut"]` tag was added, since none of it carries that tag. `tags_match: "any"`
   explicitly opts back into "tagged-OR-untagged", which is the correct semantics for a model that's
   *labeled* kubernaut-only going forward but still wants its full untagged legacy corpus.
3. **Created 8 new tag-isolated mental models** — `dcm-{testing-methodology,coding-conventions,
   architecture-decisions,workflow-preferences}` and the `engram-` equivalents — each
   `tags=["dcm"]`/`["engram"]` with the trigger's `tags_match` left at its default (`all_strict` when
   tags are set), i.e. **strict** isolation: only facts carrying that exact project tag feed these
   models, ever. Verified via `list_mental_models`/`get_mental_model` immediately after creation:
   `dcm-testing-methodology`'s first refresh returned `based_on_counts: {world: 0, experience: 0,
   observation: 0, mental-models: 3}` (the 3 being its own DCM sibling models, not kubernaut's) —
   confirms zero kubernaut leakage, at the cost of starting empty until DCM/Engram accumulate their
   own tagged content post-fix. Wired into `nightly-learn.py`'s `PROJECT_CONFIGS["dcm"]["mental_models"]`
   / `["engram"]["mental_models"]` under a `"cursor-memory"` key (mirroring kubernaut's existing
   entry) so they refresh nightly in addition to their `refresh_after_consolidation: true` trigger.
4. **Updated recall guidance** in all `hindsight-memory.mdc` rule files (kubernaut's global copy at
   `~/.cursor/rules/`, engram's own, DCM's canonical copy plus all 13 sibling DCM repos via a scripted
   replace) to: (a) pass `tags: ["<project>"]` on the mandatory-gate `recall()` call against
   `cursor-memory`, and (b) explicitly fetch the project's own 4 tag-scoped mental models via
   `get_mental_model` as authoritative context, replacing the old (DCM-absent, engram-only)
   "manually judge what's project-agnostic" mitigation.

**Known limitation / explicit tradeoff, documented in every updated rule file**: this fixes the
problem *going forward only*. The pre-existing untagged backlog (kubernaut's FedRAMP content
included) is permanently untaggable via any exposed Hindsight API, so an **unscoped** `recall()`
call (omitting `tags`) against `cursor-memory` can still surface it. The isolation guarantee only
holds when the caller explicitly passes `tags: ["<project>"]` — which is now the mandatory default
in every rule file, but is an opt-in per-call parameter, not a bank-level enforcement. DCM's and
Engram's newly-created tag-scoped mental models will also read as sparse/empty for a while (no
existing tagged content to draw from) until enough new project-tagged corrections/instructions
accumulate through normal nightly retain.

**Update (later same day): the "permanently untaggable" constraint above was wrong — retagged the
entire resolvable backlog.** The earlier diagnosis checked `PATCH /v1/default/banks/{bank_id}/
memories/{memory_id}` (memory-level) and correctly found no `tags` field there. It did not check
the sibling **document-level** endpoint, `PATCH /v1/default/banks/{bank_id}/documents/{document_id}`,
which does accept `tags` and (confirmed empirically) propagates to every memory unit derived from
that document. This reopens the "existing backlog is permanently untagged" tradeoff — most of it can
in fact be fixed retroactively.

Surveyed all 3,485 pre-existing `cursor-memory` documents by resolving each one's
`document_metadata.transcript_id` back to the Cursor workspace that produced it (same lookup
`project_for_transcript_path()` uses going forward), then to a project via
`project_scope.resolve_project_label()`:
- **2,608 resolvable** — has a `transcript_id`, the transcript file is still on disk, and its
  workspace maps to an onboarded project (2,592 kubernaut, 15 dcm, 1 engram).
- **863 not resolvable**, two distinct reasons that need different handling:
  - **469 "empty-window" sessions** — a blank Cursor window with no folder open at all. There is no
    project to attribute these to, ever; this is correct/expected untagged state, not a gap.
  - **395 no `transcript_id` at all** (`triage-rearrange`/`user-instruction` documents). Traced why:
    an earlier `triage-memories.py` pass had already deleted each one's upstream source document
    (the one that actually carried the `transcript_id`) after rewriting it — confirmed 0/390
    `original_doc` references in `document_metadata` still resolve to a live document. No amount of
    transcript-lineage tracing can recover these; they need a different signal.

**Part 1 — transcript-path retag (`backfill-memory-tags.py`, new script)**: pure planning function
`plan_retags()` (unit tested, 8 cases: kubernaut/dcm resolution, already-tagged/no-transcript_id/
missing-file/empty-window/out-of-scope-workspace all correctly skipped, mixed batch) computes the
plan from a transcript index + the current document list; a thin `apply_retag()` does the actual
PATCH. `--dry-run` first confirmed the plan matched the survey exactly (2,608 docs, same
per-project breakdown), then a live run retagged **all 2,608 in 67s with 0 failures**. Re-running
`--dry-run` immediately after confirms idempotency (plans 0 further changes) since already-tagged
documents are always skipped.

**Part 2 — content-based classification for the un-traceable 395 (`backfill-content-classified-tags.py`,
new script, using a new `classify_project_from_content()` in `spike/classify.py`)**: since these
have no transcript lineage, fall back to classifying the fact's own text with Haiku against a prompt
that requires a specific named signal per project (kubernaut: CRDs/Ginkgo/FedRAMP/SP; dcm: service
providers/OSAC/placement policy/control-plane; engram: nightly-learn/cocoindex/retain-recall) and
returns `"generic"` rather than guess when the signal is generic or absent — deliberately biased
against false positives, since a wrong guess mis-scopes a fact into the wrong project's isolated
recall, which is worse than leaving it unscoped. A `--min-confidence` gate (default 0.75, unit tested
via `should_apply_tag()`, 5 cases including the exact-boundary and error cases) only applies a tag
when Haiku both picked a real project and cleared the confidence bar. Every classification — applied
or not — is appended to an audit log (`~/.hindsight/logs/content-classification-audit.jsonl`) for
manual review. Result on the live backlog: **144 tagged** (133 kubernaut, 10 dcm, 1 engram) and
**251 left untagged** (generic or below the confidence gate) out of 395 — the conservative default
correctly declined to guess on the majority. 4 calls hit a transient DNS error on the first run
(`nodename nor servname provided`); a second run (the script is idempotent — only re-targets
still-untagged documents) resolved all 4 with no further errors, landing them in "left untagged"
rather than forcing a guess.

**Verification**: `recall(query="project conventions and architecture decisions", tags=["dcm"],
tags_match="all_strict")` against the live bank now returns exclusively DCM-tagged results (OSAC
service provider, `dcm-project/enhancements`, control-plane/environment-agent phasing) — zero
kubernaut/FedRAMP content, confirming the isolation guarantee now holds for raw-fact recall too, not
just the mental models. Final bank state: 3,487 total documents, 2,744 project-tagged (2,724
kubernaut / 18 dcm / 2 engram — the small excess over the 2,608+144=2,752 backfilled count is live
traffic retained mid-backfill, already correctly tagged by the 2026-07-27 `retain_windows()` fix), 729
correctly-untagged (469 empty-window + ~251 low-confidence content + a handful of same-day new
arrivals not yet triaged).

**Update (same evening): two more gaps found while asking "can the remaining 729 be attributed?"**

1. **"Empty-window" was the wrong assumption to write off.** Sampling 12 random empty-window
   documents found every single one full of clear per-project signal (`jordigilh/kubernaut` PR/issue
   URLs, "kubernaut-operator", "SP CRD", GA-gate checklists) — the workspace label reflects a Cursor
   session-recording quirk (no folder open *at that moment*, e.g. a duplicate/orphaned copy of a
   transcript that also exists correctly-attributed elsewhere), not an actual absence of project
   context in the conversation itself. `backfill-content-classified-tags.py`'s `plan_content_targets()`
   was widened to target every untagged document NOT resolvable by `backfill-memory-tags.py`'s
   `plan_retags()` — covering both the no-`transcript_id` bucket and the empty-window bucket in one
   pass — rather than treating "workspace unresolvable" as "permanently unattributable."
2. **`cocoindex-flows.py`'s `process_transcript()` never got the `retain_windows()` project-tagging
   fix.** It resolves `project` and forwards it to `contradiction_resolution.resolve()`, but never
   wrote it onto the retained item's own `tags` — a second, independent retain path with the exact
   same gap the original fix closed, just not in the file that was actually being read that day. Caught
   because 9 same-day `dcm-project/osac-service-provider` and `kubernaut-v1-5` documents showed up
   untagged despite a cleanly resolvable workspace — new pollution being generated in real time even
   after the "fix" landed. Patched to build `tags = [project] if project else []` the same way
   `retain_windows()` does (3 new regression tests), and restarted `io.vectorize.cocoindex.service`
   (confirmed via the `~/.hindsight/cocoindex-flows.py` symlink that this is the daemon serving the
   file) so the fix is live for future traffic. The 9 pre-existing untagged documents were swept up by
   a normal `backfill-memory-tags.py` re-run once the underlying transcripts existed on disk.

**Result of running the widened content classifier against the full un-resolvable backlog (708
documents this pass, some already having failed classification in the earlier 395-doc pass and
correctly re-attempted)**: 484 more documents tagged (309 kubernaut, 28 dcm, 3 engram from the main
run, +4 kubernaut from a cleanup re-run), for **628 total tagged via content classification** across
both passes. 12 documents hit a *different* failure mode than the earlier run's transient DNS
errors — Haiku occasionally responds with prose instead of the required JSON object when a fact's
own text contains an embedded code block or quoted conversation that confuses it into "explaining"
rather than classifying; all 12 are logged as errors (not force-tagged) and were not worth chasing
further for a one-off backfill.

**Final bank state**: 3,133/3,492 documents tagged (**89.7%**, up from the initial 79% after the
first backfill), 359 left untagged. Manually sampling the final 359 confirms the conservative
confidence gate is doing its job correctly — nearly all are genuinely project-agnostic (UX/roadmap
preferences, generic Go/Kubernetes patterns, generic project-management instructions) or, in a
handful of cases, from a conversation about an entirely different, never-onboarded project (e.g. a
Django-migration discussion, presumably `koku`/`insights-onprem` bleeding through an empty-window
session — the same category `purge-out-of-scope-memories.py` already handles for
transcript-resolvable documents, just not reachable by that script's transcript-path check here).
**Decision: leave the 359 as-is, do not bulk-delete.** `cursor-memory`'s explicit design intent is a
shared bank for universal coding-hygiene lessons (see `docs/NEW_PROJECT_SETUP.md`), so untagged-but-
genuinely-generic is the *correct* end state, not leftover noise — and `purge-out-of-scope-memories.py`
already established the precedent of only deleting *confirmed* out-of-scope content, never deleting
merely because attribution is uncertain. Identifying and removing the small number of confirmed
off-topic (e.g. Django/koku) documents within the 359 remains a possible narrow follow-up, but is a
distinct, lower-stakes decision from the tagging work done here and wasn't executed without an
explicit ask.

**Update (later same evening): ran that narrow off-topic cleanup — and caught a classifier
false-positive risk plus a live leaked secret along the way.**

Added `purge-confirmed-off-topic-memories.py` (content-based sibling of `purge-out-of-scope-memories.py`,
which only handles transcript-resolvable documents): a new, higher-bar `classify_off_topic_content()`
in `spike/classify.py` (min confidence 0.85 vs. tagging's 0.75, since deletion is irreversible) audits
each of the 356 still-untagged documents and asks "is this CONFIRMED to be about a different, named,
unrelated project" rather than "which of our 3 projects is this". Dry run flagged **85/356 (24%)**.

**Manual review before executing found the classifier over-flagging bare acronyms/internal jargon it
simply didn't recognize as "confirmed off-topic":** 34 of the 85 were vague labels like `RO`
("Requirement/Rule Orchestration"), `WE`, `AA`/`KA`, `HAPI`, `AF`, `BR-ORCH-*`, `BR-INTERACTIVE-010`,
and "GA Readiness Audit" — several of which strongly resemble kubernaut's own internal component
names (Remediation Orchestrator, Workflow Engine, AI Analysis) rather than external systems; the
model's own "default to not off-topic when uncertain" instruction didn't hold in practice for jargon
it lacked context to place. Manual full-text read of even the "concrete/named" bucket caught 3 more:
`Cursor` (the IDE itself — cross-project infra, and literally Engram's own subject matter),
`FleetConfig` (plausibly DCM's own fleet-control-plane concept, given an already-tagged DCM document
elsewhere references "FMC serves as the fleet control plane"), and `AEP`/`spectral` (generic
API-linting tooling any of the 3 projects could plausibly use). Presented the full breakdown to the
user via `AskQuestion` rather than executing blind; user chose the conservative option.

**Executed only a manually-curated 47-document subset** — every one backed by an explicit repo
name, URL, or PR/branch reference for a genuinely different, unrelated project: `koku` (20, cost
management platform — matches the exact out-of-scope workspace already known from the 2026-07-13
`purge-out-of-scope-memories.py` incident), `kessel`/`Kessel` (7), `insights-onprem` (3), `kagenti`
(2, explicit GitHub PR links), `RHDH`/Red Hat Developer Hub (2), `insights-rbac`, `AuthBridge`/
`OpenShell`, `goose` (explicit PR #1682/branch name), `AWX`, `ros-ocp-backend`, `parodos`,
`holmesgpt-api`, `Kuadrant`, `community-operators`, `cost-onprem`/`CMMO`. Deleted via direct
`contradiction_resolution.delete_document()` calls against the exact 47 IDs (not a fresh classifier
re-run, to guarantee the curated list — not a possibly-different reproduction of it — is what gets
deleted). 47/47 succeeded. The 34 bare-acronym/vague-label documents plus `Cursor`/`FleetConfig`/
`AEP`-`spectral` were left untouched.

**Separately, one flagged document (`triage-4bdf77c8-...`) turned out to contain the actual leaked
GCP project ID (`itpc-gcp-eco-eng-claude`) in cleartext** — the same value already purged from this
repo's git history (see the 2026-07-19/21 entries). Rather than fold it into the routine off-topic
batch delete, scanned the *entire* live bank's chunk text (not just the untagged backlog) for that
literal string and found **6 documents total**, not just the 1 the classifier happened to flag —
the other 5 were tagged (in-scope, since the conversations were genuinely about configuring
Engram/kubernaut's own Vertex AI setup) and so were invisible to an off-topic-only sweep. All 6
deleted via a separate one-off script/log (`~/.hindsight/logs/leaked-secret-purge.jsonl`), and a
full re-scan of the resulting 3,445-document bank confirmed zero remaining occurrences of the
string. This closes a gap the original git-history purge couldn't reach: retained memory content is
a separate store from git history and needed its own remediation pass.

**Final bank state after both purges**: 3,445 total documents (down from 3,492), all 53 deletions
logged across two separate audit trails (`off-topic-purge-audit.jsonl` for the 47,
`leaked-secret-purge.jsonl` for the 6). The remaining ~312 untagged documents are left as-is per the
original decision above (genuinely generic content, or ambiguous jargon not worth a forced call).

## 2026-07-28: Transient DNS Outage During Nightly Start Window — `reflect()` Failed for Both Projects, Mental Model Refresh Was Unaffected

**Trigger**: Routine "status report from last night's run" check. Both `hindsight.nightly` (kubernaut)
and `hindsight.nightly-dcm` jobs completed without crashing, but `reflect_result` in both
`2026-07-28.json`/`2026-07-28-dcm.json` showed `{"error": "<urlopen error [Errno 61] Connection
refused>"}`.

**Root cause**: `hindsight-api` was crash-looping (repeated "Application startup failed. Exiting.")
from before 15:07 until 15:30 today, caused by transient DNS resolution failures reaching
`oauth2.googleapis.com` and HuggingFace's hub (`nodename nor servname provided, or not known`) —
the same network blip responsible for a handful of transient LLM-call errors in that afternoon's
memory-tagging backfill scripts (see the two entries above). Both nightly jobs happened to start
their bank-stats/reflect/recall-probe phase inside that exact 23-minute window, so all three failed
with connection-refused for both projects. `reflect()`'s failure is caught and stored as
`{"error": ...}` (see `nightly-learn.py`'s existing try/except, added after the 2026-07-26
unguarded-API-call crash) rather than crashing the job, so everything downstream — transcript scan,
mental model refresh, triage, dedup, dashboard/pending-contradictions regen — ran normally once the
API recovered by 15:30, all still inside the same ~44-minute run. Confirmed via
`launchd-stderr.log`/`launchd-dcm-stderr.log`: every configured mental model (`kubernaut-issues/*`,
`cursor-memory/*` incl. all 4 kubernaut-scoped ones, `kubernaut-docs/*` for kubernaut;
`dcm-docs/*`/`dcm-issues/*`/`cursor-memory/dcm-*` for dcm) shows "refresh triggered" at 15:31-15:32
(dcm) and 15:50-15:51 (kubernaut) — well after the API had recovered.

**Net impact was narrower than it first looked**: only the single, bank-scoped `reflect()` call (a
one-shot "top 3 recurring correction patterns" query against `cursor-memory`, unrelated to mental
model consolidation despite running in the same phase of the script) was actually missed — mental
model refresh, the more consequential nightly step, was unaffected.

**Fix**: manually re-ran `nl.reflect()` once the API was confirmed healthy and patched the result
directly into both `2026-07-28.json` and `2026-07-28-dcm.json` (`reflect_result` +
`reflect_backfilled_at` marker) so the historical record isn't missing that night's correction-pattern
synthesis. No code change needed — the existing try/except already degrades gracefully exactly as
designed; this was a genuine transient environmental outage, not a bug.

## 2026-07-27: Every Production Haiku Correction-Detection Call Had Been Failing — Missing `VERTEXAI_PROJECT` in launchd Envs, Not Just the Wrong Region

**Context**: Investigating the 0% recall-adoption dip surfaced a `backfill-effectiveness.py`
dry run in which every Haiku correction-classification call failed with
`litellm...AnthropicError: Publisher Model ... is not servable in region us-central1`. That
looked like a simple region misconfiguration and `spike/classify.py`'s `VERTEXAI_LOCATION`
`setdefault()` was changed from `us-central1` to `global` to fix it (confirmed working via a
manual test using the interactive shell's ambient GCP env).

**That fix was necessary but not sufficient — a much bigger bug was hiding underneath it.**
Re-testing `classify_correction()` with a genuinely clean environment (no ambient shell
exports at all, matching what a launchd job actually sees) surfaced a *different* error:
`403 PERMISSION_DENIED: Permission denied on resource project example-gcp-project`. That
placeholder value comes from `spike/classify.py`'s own `os.environ.setdefault("VERTEXAI_PROJECT",
"example-gcp-project")` — the generic placeholder substituted in commit `40afb75` when the real
project ID was scrubbed from this public repo (see the 2026-07-19/21 entries below). The
`setdefault()` design assumes the real value is already exported in the calling process's
environment (documented right above it in `spike/classify.py`) — true for an interactive shell
that happens to have `ANTHROPIC_VERTEX_PROJECT_ID` (a *different* env var name that some litellm
code paths fall back to) set, but **false for every launchd job**.

**Root cause, confirmed by inspecting every `launchd/*.plist` `EnvironmentVariables` block**:
- `io.vectorize.hindsight.service.plist` (the API server) and `io.vectorize.prefilter-shadow-trial.plist`
  *did* set `VERTEXAI_PROJECT`/`GOOGLE_CLOUD_PROJECT`/`VERTEXAI_LOCATION` correctly (via the
  existing `__VERTEXAI_PROJECT__` sed-substitution pattern in `docs/INSTALL.md`).
- `io.vectorize.hindsight.nightly.plist`, `io.vectorize.hindsight.hourly.plist`,
  `io.vectorize.hindsight.nightly-dcm.plist`, and all three `io.vectorize.cocoindex.*.plist`
  jobs had **no Vertex AI env vars at all**. Every `nightly-learn.py` and `cocoindex-flows.py`
  process — i.e. the actual production write path for retain/correction-detection, as opposed to
  the shadow-trial's own isolated, correctly-configured job — ran with the fake placeholder
  project, and every `classify_correction()`/`check_contradiction()` call inside it had been
  hitting a hard 403 since `correction_gate.py` went live.

**Compounding bug**: `correction_gate.py`'s `classify_cached()` cached *every* result from
`classify_correction()`, including error results, with no error-awareness or TTL. So each 403
failure got permanently baked into `~/.hindsight/logs/correction-cache.json` as `is_correction:
false` for that exact message text — a silent, permanent false negative that would never be
retried even after the underlying config was fixed. The cache had grown to 35,660 entries (91%
`False`); the `True` entries are unaffected by this bug (a 403 always yields `is_correction=False`
before this fix, so `True` entries in the cache are provably genuine), but the `False` entries were
an unknowable mix of real non-corrections and outage-poisoned false negatives.

**Fix** (four parts):
1. `spike/classify.py`: `VERTEXAI_LOCATION` default changed `us-central1` → `global` (confirmed
   working for `claude-haiku-4-5@20251001`, matching `~/.hindsight/config.env`'s working config
   for the same model).
2. `correction_gate.py`: `classify_cached()` no longer caches a result when `classify_correction()`
   returns an error — each call gets an uncached `False` for *that* call only, so the next call
   with the same text retries Haiku instead of trusting a poisoned cache entry.
3. **New**: `with-config-env.sh`, a shared launchd wrapper (mirroring `start.sh`'s existing
   `set -a; source config.env; set +a` pattern) that every LLM-touching `launchd/*.plist` now
   runs through as the first `ProgramArguments` entry, injecting `config.env` into the process
   environment **at runtime** instead of baking `VERTEXAI_PROJECT`/model names into any plist —
   including the local, non-git ones under `~/Library/LaunchAgents/`. Applied to all 8 affected
   plists: `hindsight.service`, `hindsight.nightly`, `hindsight.hourly`, `hindsight.nightly-dcm`,
   `cocoindex.service`, `cocoindex.engram`, `cocoindex.dcm`, `prefilter-shadow-trial`. This also
   retired the old `__VERTEXAI_PROJECT__` sed-substitution pattern in `docs/INSTALL.md` — no
   plist, committed or local, contains any LLM/project configuration now; `config.env` is the
   single source of truth. `docs/INSTALL.md` and `config.env.example` updated accordingly
   (`config.env.example` also gained the `FORCE_CPU`/`DB_POOL_*` keys that had only ever existed
   hardcoded in the local plist, never in the example).
4. Backed up and cleared `correction-cache.json` (35,660 entries) so classification restarts
   fresh under the fixed config rather than continuing to trust unreliable cached `False` results.

**Verification**: `~/.hindsight/with-config-env.sh env` (via `env -i` with only `HOME`/`PATH` —
no ambient shell exports) confirmed `VERTEXAI_PROJECT`/`GOOGLE_CLOUD_PROJECT`/`VERTEXAI_LOCATION`/
`GOOGLE_APPLICATION_CREDENTIALS` are all correctly populated at runtime; a subsequent
`classify_correction()` call in that same clean environment succeeded (`error=None`,
correctly classified a real test correction as `is_correction=True,
category=convention_violation`). All 8 plists reloaded via `launchctl bootout`+`bootstrap`; all
long-running (`KeepAlive`) services (`hindsight.service`, `cocoindex.service`, `cocoindex.engram`)
confirmed `running` and `hindsight-api` `/health` returned 200 post-reload. 214/214 tests pass,
including a rewritten `test_error_result_is_treated_as_not_a_correction_but_not_cached` regression
test.

**Impact / open question**: it is not yet known how long this had been broken or how many real
corrections were silently dropped from the production retain pipeline as a result — the shadow
trial's own numbers (e.g. the regex-vs-Haiku comparison, prompt v1→v2 tuning) are unaffected since
that job had its own correctly-configured plist all along. This is a strong argument for the
`with-config-env.sh` consolidation: a single source of truth removes an entire class of
"one plist out of eight was configured differently" bugs going forward.

---

## 2026-07-26: Upgraded `hindsight-api` 0.8.4→0.8.5 and `cocoindex` 1.0.16→1.0.18

**Why**: Routine release check turned up two concrete reasons to upgrade rather than just take
the latest for its own sake:

- **`hindsight-api` 0.8.5 ships our own deadlock-fix saga.** PR #2529 (the fix originally
  submitted from this project, see the 2026-07-1x deadlock investigation entries below) was
  closed as superseded by the maintainer's own follow-up, **PR #2534** ("catch the #2529 sweep
  deadlock in continuous perf, and drive dropped passes to zero"), merged upstream 2026-07-20 and
  shipped in the 0.8.5 release (2026-07-22). 0.8.5 also bundles a second, independent deadlock fix
  (**PR #2570**, "Fix chunk delete deadlock ordering").
- **`cocoindex` 1.0.17 has an actual security fix**: pyo3 0.27→0.29 upgrade to resolve security
  advisories, plus a Postgres fix directly relevant to our pgvector usage ("recursively strip
  NUL/U+0000 from array and composite bindings"). 1.0.18 (current latest) adds only
  lower-priority features/docs on top (Dart tree-sitter grammar, `cocoindex show` inspection
  improvements).

**Upgrade procedure** (matches the documented runbook in `docs/INSTALL.md`):
```
uv pip install --python ~/.hindsight/venv/bin/python -U cocoindex
uv pip install --python ~/.hindsight/venv/bin/python -U 'hindsight-api[all]'
```
Note: the second command printed `warning: The package hindsight-api==0.8.5 does not have an
extra named 'all'` — benign; the `[all]` extras group was apparently restructured/removed in this
release, but `hindsight-api-slim` is an unconditional (non-extra) dependency of `hindsight-api`,
so it upgraded to 0.8.5 regardless. Confirmed via `uv pip show`: `cocoindex==1.0.18`,
`hindsight-api==0.8.5`, `hindsight-api-slim==0.8.5`.

**Rollout**: restarted `io.vectorize.cocoindex.service`, `io.vectorize.cocoindex.engram`, and
`io.vectorize.cocoindex.dcm` first (lower risk, no DB migrations) — all three reconnected to a
healthy Hindsight API cleanly. Then restarted `io.vectorize.hindsight.service` via `launchctl
kickstart -k`; `hindsight-stdout.log` showed `Database migrations completed successfully`
followed by normal startup. One transient `asyncpg.exceptions.ForeignKeyViolationError` on
`observation_history` appeared in stderr during the restart window itself — this is an in-flight
worker task getting SIGTERM'd mid-transaction by the forced restart, not a migration/upgrade
regression (confirmed: `/health` returned `{"status":"healthy","database":"connected"}` within
seconds, and subsequent `graph_maintenance` runs — the exact code path PR #2534 patches —
completed cleanly with real work done, e.g. `relink_units_processed: 48, relink_links_added:
1489`, no deadlock errors).

**Not done**: no version pins exist anywhere in this repo for either package (`docs/INSTALL.md`
intentionally documents unpinned `pip install`/`uv pip install`), so there's nothing to bump in
git beyond this findings entry.

## 2026-07-23: Nightly Run Crashed Entirely on a Transient Connection Blip — One Unguarded API Call

**Context**: User asked for the status report from "yesterday's run". `launchctl list` showed both
`io.vectorize.hindsight.nightly` and `io.vectorize.hindsight.nightly-dcm` with a last exit code of 1,
and neither `2026-07-23.json` nor `2026-07-23-dcm.json` existed — the entire nightly pass for both
projects had produced no output at all.

**Timeline**: Both jobs are scheduled via `StartCalendarInterval` at 2:00 AM / 2:30 AM, but
`launchd-stderr.log` / `launchd-dcm-stderr.log` showed them actually starting at **04:04:43** and
**04:30:00** — over two hours late. `pmset -g log` showed why: **147 sleep/wake cycles since boot**
(2026-07-21 11:36:15), and the jobs only fired once the machine next woke, which happened to land on
a low-power "MaintenanceWake" (Power Nap) event rather than a full interactive wake. At that exact
moment, every `GET`/`POST` to `http://localhost:8888` returned `ConnectionRefusedError: [Errno 61]` —
Hindsight's port was transiently unreachable for roughly the first minute of each run, even though the
underlying `hindsight-api` process (PID confirmed via `ps -o lstart`) never actually restarted across
that window and was confirmed healthy again minutes later with no intervention. Best working theory:
Power Nap wakes bring the CPU up but don't necessarily fully restore network stack state before
launchd's calendar jobs fire, unlike a full user-initiated wake — not confirmed at the kernel level,
but consistent with every other symptom (no crash, no restart, self-resolving, correlates with the
sleep/wake count).

**The transient blip alone shouldn't have mattered** — nearly every API call in `nightly-learn.py`
already wraps `api_post()`/`api_get()` in try/except and logs a warning-and-continue on failure
(stats collection, reflect, recall probes all did exactly that in the logs, and the run kept going:
transcripts were found, corrections were analyzed, effectiveness stats were computed with real
numbers). **The actual bug**: the final phase of `run_nightly()` — the loop that refreshes every
mental model — called `api_post()` directly with no try/except, the one place in the whole file that
didn't follow the file's own established defensive pattern. Because `api_post()` always re-raises
(`HTTPError`/`URLError`) by design (so its callers can choose how to handle failure), that single
unguarded call threw an unhandled exception through `main()` and killed the entire process — discarding
every effectiveness/session/probe metric that had already been successfully computed earlier in that
same run, for both projects, on top of losing the mental-model refreshes themselves.

**Fix**: wrapped the mental-model refresh loop in the same `try/except (HTTPError, URLError):
log.warning(...); continue` pattern already used everywhere else in the file
(`nightly-learn.py`, `run_nightly()`, ~line 1628). Deployed instantly since `~/.hindsight/nightly-learn.py`
is a symlink into this repo — no service restart needed, since `launchd` invokes `python3
nightly-learn.py` fresh on each scheduled run rather than running a long-lived daemon.

**Backfill**: rather than wait for tonight's scheduled run, manually re-ran
`nightly-learn.py --mode nightly --project kubernaut` and `--project dcm` by hand once Hindsight's API
was confirmed healthy. Both completed cleanly (`2026-07-23.json`, `2026-07-23-dcm.json`,
`docs/DASHBOARD.md`, `docs/PENDING_CONTRADICTIONS.md` all regenerated) — no data gap in the historical
record. `launchctl list` continues to show the stale exit-code-1 from the 4 AM failure until tonight's
run overwrites it; this is cosmetic (launchd only remembers the last invocation it itself started) and
not a sign of an ongoing problem.

**Not fixed (environmental, not code)**: the underlying cause of *why* the loopback connection was
refused for that first minute after a Power-Nap-triggered wake remains unconfirmed — this is the same
class of "networking not fully back yet right after sleep/wake" issue documented elsewhere in this file
for Vertex AI/GitHub over Wi-Fi, just manifesting on loopback this time. 147 sleep/wake cycles in under
48 hours is unusually high and worth keeping an eye on (lid-open/close pattern, or a misbehaving Power
Nap trigger) since it directly caused both the 2-hour scheduling delay and the connectivity window that
exposed this bug. The defensive-coding fix above means this class of transient blip can no longer take
down an entire nightly run regardless of root cause, which was the actionable part.

## 2026-07-21 (evening): Hindsight Wouldn't Start After a Reboot — `pg0`'s Stale-PID Liveness Check

**Context**: User reported "hindsight is failing to start". `hindsight-stderr.log` showed a
`psycopg2.OperationalError: connection to server at "127.0.0.1", port 5432 failed: Connection
refused` on every restart attempt, inside `hindsight_api.migrations.run_migrations` — the app itself
was fine, its database just wasn't there.

**Investigation, including a wrong turn**: `lsof -i :5432` showed nothing listening. First
hypothesis: Homebrew's `postgresql@16` had been uninstalled (Cellar entry gone, no binaries on
`PATH`) despite its data directory (`/opt/homebrew/var/postgresql@16`, last touched 2025-09-23)
still existing — no trace of *when*/*how* this happened (no shell history, no Homebrew logs).
Reinstalled it and started it — **wrong fix**. Hindsight doesn't use system Postgres at all; it uses
`pg0-embedded`, a self-contained Postgres distribution (own bundled Postgres 18.1.0 binary, own data
directory under `~/.pg0/instances/hindsight/data`, own `instance.json` tracking pid/port/creds) that
`hindsight_api.pg0.EmbeddedPostgres` starts/manages on every app startup (default `DATABASE_URL =
"pg0"`, resolved via `resolve_database_url()`). The `postgresql@16` reinstall was for a completely
unrelated, never-before-used cluster; worse, starting it squatted on host port 5432 and produced a
*different* wrong error ("role hindsight does not exist") until it was stopped again. Uninstalled it
afterward to avoid this dead end recurring. (There's also a second, genuinely orphaned leftover at
`~/.hindsight/data/instances/hindsight/` — 6.9GB, PG 18 data last written 2026-07-12, not referenced
anywhere in code/config/plists — likely a relic of an earlier pg0-embedded default-path change.
Left in place; flagged for the user to decide whether to reclaim the disk space.)

**Actual root cause**: `pmset -g log` showed the Mac rebooted at **22:36:15**, ~3 minutes after
Postgres received a clean "smart shutdown request" at 22:33:30 (visible in
`~/.pg0/instances/hindsight/data/log/postgresql-2026-07-21.log` — a graceful shutdown consistent with
the OS terminating background daemons before a restart, not a crash). On boot, `launchd`
(`RunAtLoad`) restarted `hindsight-api` fresh, which calls `EmbeddedPostgres.ensure_running()` →
`is_running()` → `pg0.info()`. That check only verifies *some* process currently holds the PID
recorded in `~/.pg0/instances/hindsight/instance.json` (last written when Postgres actually started,
2026-07-12) — it doesn't check that the process is actually `postgres`. After a reboot, low PIDs get
reassigned quickly; the recorded PID (2691) had been reused by an unrelated system process
(`TextInputMenuAgent`). So `is_running()` false-positived, `ensure_running()` skipped ever calling
`start()` (confirmed: `pg0.py`'s "Starting embedded PostgreSQL..." log line never once appears in
`hindsight-stdout.log`), and the app just kept retrying a connection to a database that was never
actually launched — a crash loop that would have continued indefinitely without manual intervention.
Reproduced directly: `pg0 start --name hindsight` (the CLI, same underlying check) refused with
`Error: Instance already running (pid: 2691)`.

**Fix**: backed up `instance.json`, corrected its `pid` field to a clearly-invalid value so the
liveness check would fail honestly, then ran `pg0 start --name hindsight` for real. It came up as a
*fresh* setup (Postgres 18 binary, same existing `data_dir` since that's pg0's own default — data was
never touched, verified table-by-table afterward) but overwrote `instance.json`'s
username/password/database to the CLI's generic defaults (`postgres`/`postgres`/`postgres`) since
those weren't passed explicitly. Confirmed the actual `hindsight` role/database/tables were untouched
by connecting directly (`psql -U hindsight -d hindsight`), then corrected `instance.json`'s
credentials back to `hindsight`/`hindsight`/`hindsight` to match what `hindsight_api.pg0.py` always
requests. Restarted `io.vectorize.hindsight.service` — clean migration run, `{"status":"healthy",
"database":"connected"}`, worker poller and maintenance sweep all came up normally.

**Not fixed (upstream bug, not ours to patch)**: `pg0-embedded`'s liveness check trusting a bare PID
number with no identity verification (e.g. checking `/proc/<pid>/comm` or the process's command line
contains `postgres`) is a real bug in the third-party package, not in Engram/Hindsight's own code —
nothing to commit here beyond this writeup. Worth watching for a `pg0-embedded` release that fixes
this, since the same failure mode will recur on every future reboot/sleep-triggered shutdown that
happens to land while nothing has proactively restarted the embedded instance since.

**Lesson**: "connection refused on 5432" doesn't necessarily mean "the expected Postgres died" — check
which Postgres is expected first (`grep DEFAULT_DATABASE_URL`/`pg0`/embedded-db config) before
reaching for the system package manager. And a service reporting itself "running" based on stale
bookkeeping (PID reuse after reboot) is a general reminder that `KeepAlive`/`RunAtLoad` daemons need
liveness checks with actual identity verification, not just existence checks — same category of bug
as the 2026-07-21 (morning) stale-cocoindex-daemon incident below, different manifestation.

## 2026-07-21: The `project=null` Fix Was Never Live — Stale `KeepAlive` Daemon, Then a Real Bug Once Restarted

**Context**: Routine status check found all 37 entries queued in `contradictions-pending.jsonl`
since the 2026-07-19 reset still had `project: null` — the exact bug that was supposedly fixed two
days earlier (commit `e826cc0`).

**Root cause #1 — fix was never deployed to the running process.** `nightly-learn.py` runs as a
scheduled `launchd` job: a fresh process every time, always reads the latest code from disk.
`cocoindex-flows.py`'s `transcripts-app` (the thing that actually calls
`contradiction_resolution.resolve()` for every live session in practice — the nightly logs
consistently show "0 transcripts, 0 corrections" because cocoindex already got there first) runs
as a **long-lived `KeepAlive` daemon** (`io.vectorize.cocoindex.service` / `.cocoindex.engram`,
`RunAtLoad`+`KeepAlive`, no periodic restart). `ps -o lstart` showed both had been running
continuously since **Jul 17, 20:18** — before the fix even existed — and a `KeepAlive` daemon only
reloads its module once, at process start; editing the (symlinked) source file on disk does nothing
until the process is killed and relaunched. Same gap silently applied to an unrelated fix from the
same day (`559fcf9`, a `correction-cache.json` save race) — anything touching modules this daemon
imports needs an explicit restart to actually take effect, and nothing enforces or reminds anyone of
that.

**Root cause #2 — the fix itself had a bug, caught only by restarting.** `launchctl kickstart -k`
on both daemons immediately surfaced `AttributeError: 'PurePosixPath' object has no attribute
'resolve'` in `process_transcript()`. The 2026-07-19 fix used `file.file_path.path.resolve()`, but
per cocoindex's actual `FilePath` class (`cocoindex/resources/file.py`): `.path` is only the
**relative** path (a bare `PurePosixPath`, no filesystem methods at all), while `.resolve()` on the
`FilePath` object itself (not on `.path`) is what returns the absolute concrete `pathlib.Path`. The
two other pre-existing call sites in the same file (`process_doc_file` etc.) already used the
correct `file.file_path.resolve()` — this fix just used the wrong one. The unit test's `FakeFile`
mock didn't catch it because it modeled `file_path` as a bare `SimpleNamespace(path=<concrete
Path>)`, so `.path.resolve()` "worked" in the mock despite not existing on the real type. This is
exactly the class of bug integration/E2E tests catch and unit tests with inaccurate mocks don't —
there was no test that exercised this code path against anything resembling the real cocoindex API.

**Fix**:
1. Corrected `cocoindex-flows.py` back to `file.file_path.resolve()`, matching the two already-correct
   call sites elsewhere in the file.
2. Fixed `FakeFile` in `tests/test_cocoindex_flows.py` to actually mirror cocoindex's contract:
   `.path` is a relative `PurePosixPath` (used only for `.stem`), `.resolve()` is a separate callable
   returning the absolute `Path`. Verified the corrected test actually has teeth by temporarily
   reintroducing the bug and confirming the 3 project-tagging tests fail with the same
   `AttributeError` seen in production, then re-fixed and re-verified green (193/193).
3. Restarted both daemons again (`launchctl kickstart -k`) and confirmed clean startup: no errors in
   `cocoindex-stderr.log`, and a `claude-sonnet-4-6` LiteLLM call (the contradiction-check model)
   fired successfully shortly after — confirming `process_transcript()` is executing end-to-end again.
4. Left the 37 already-queued `project: null` entries alone (documented, not backfilled) — same
   reasoning as the 2026-07-19 backlog decision: no reliable way to retroactively resolve project
   for entries that never recorded it, and the code path producing new ones is fixed going forward.

**Lesson**: when a fix touches code imported by a long-running `KeepAlive` daemon, "committed" does
not mean "deployed" — always check `ps -o lstart` against the fix's commit timestamp, or just
restart proactively, before declaring a fix live. Second: mocks for third-party library types should
be verified against the real type's actual interface (or exercised in an integration test at least
once) rather than assumed from how the code *reads* — this mock had been accepted into the codebase
and passed CI for 2 days while being subtly wrong.

**Follow-up (same day)**: rather than leave the 37 `project: null` entries sitting in the queue
indefinitely, dropped them outright (backed up to
`contradictions-pending.jsonl.bak-untagged-20260721-084846` first). Both daemons are now confirmed
running the fixed code, so any of these that are still live contradictions will resurface on their
own next time cocoindex processes the relevant transcript window — this time tagged with the correct
project. Regenerated `docs/DASHBOARD.md`/`docs/PENDING_CONTRADICTIONS.md` to reflect the empty queue.

## 2026-07-19: Real GCP Project ID Scrubbed From Git History (Not Just HEAD) After Org-Wide Leak Sweep

**Context**: A separate team ran an org-wide sweep for leaked Vertex AI project identifiers across
several repos and had already pushed a fix directly to Engram's `main` (commit `40afb75`,
co-authored with Cursor): 3 spike scripts' hardcoded `VERTEXAI_PROJECT`/`GOOGLE_CLOUD_PROJECT`/
`VERTEXAI_LOCATION` `os.environ.setdefault()` fallbacks replaced with generic placeholders, and
`triage-memories.py`'s hardcoded dedup literal moved to an opt-in `ENGRAM_GCP_PROJECT` env var.
Asked to review it.

**Review findings**:
- The code changes themselves were correct: `setdefault()` semantics preserved (a real exported
  value always wins), placeholders are non-identifying, comments explain the behavior, all 193 tests
  still pass. Confirmed via full-repo grep that no other file (config, docs, launchd plists) had ever
  hardcoded the real value — `config.env.example` and the `launchd/*.plist` templates already used
  safe placeholders (`your-gcp-project-id`, `__VERTEXAI_PROJECT__`) substituted at install time from
  `~/.hindsight/config.env`, so this leak was isolated to exactly the 4 changed files.
- **But the fix only scrubbed HEAD.** `git log --all -S "<real-project-id>"` found the value still
  present in 3 earlier commits' diffs, and — worse — **in the fix commit's own message**, which
  restated the real value in plain text ("Replace \<real-project-id\> / global hardcoded
  fallbacks..."). Since Engram is a **public** GitHub repo (confirmed via `gh repo view`), that's a
  live, permanent leak: scrubbing the tip of `main` does nothing to a value anyone can retrieve via
  `git log -p`, `git blame`, or GitHub's own commit-history UI on those older commits. A commit
  message is arguably worse than a diff here, since it's visible directly in `git log`/GitHub's
  commit list without opening anything.

**Fix**: confirmed 0 forks/stars/network on the repo (low blast radius), then rewrote history:
1. Backed up the full repo (`git clone --mirror`) before touching anything.
2. Made a fresh `--no-local` clone (git-filter-repo requires this, or `--force`, to guard against
   accidentally mangling the repo you're standing in).
3. Ran `git filter-repo --replace-text <file> --replace-message <file>` with a single literal
   mapping (`<real-project-id>==>example-gcp-project`) — `--replace-text` handles blob content,
   `--replace-message` (a separate flag) handles commit/tag messages, which the diffs-only fix had
   missed entirely.
4. Verified clean via three independent checks: `git log --all -S`, `git log --all --grep`, and a
   brute-force `git grep` across every blob in every commit (`git rev-list --all | xargs git grep`).
   Also diffed the full working tree between old and rewritten clones (identical modulo gitignored
   `__pycache__`) and re-ran the test suite (193/193) to confirm the rewrite didn't corrupt anything.
5. Force-pushed the rewritten history to `origin/main`, then hard-reset the local clone to match.

**Not done**: didn't touch `spike/__pycache__/*.pyc` (stale bytecode cache with the old value baked
in) — it's gitignored, was never committed, and Python recompiles it automatically once source
changes, so it's not a real exposure. Didn't add test coverage for `triage-memories.py`'s dedup
logic — it had zero coverage before this fix too; out of scope for a review of someone else's
security patch.

**Lesson**: "remove the secret from the file" and "remove the secret from the repo" are different
tasks once anything has been pushed to a public remote — the first only stops the leak from getting
worse, the second requires touching history (and checking commit messages specifically, not just
diffs, since tools like `git filter-repo` require a separate flag for messages). Worth checking
whether the other repos this same team touched in the org-wide sweep got a HEAD-only fix or an actual
history rewrite.

## 2026-07-19: Pending-Contradictions Backlog Reset to Zero — Only 2 of 196 Were Ever Actually Reviewed

**Context**: Asked for a status update; the report showed **196 pending contradictions, 0 resolved**.
User recalled having "already processed them" — investigated to confirm whether that memory matched
reality before deciding what to do.

**What actually happened, reconstructed from `docs/FINDINGS.md`'s own history**: the *only* real
human review ever done was the 2 entries from the 2026-07-12 "first live run" (see that date's
entry) — both confirmed false positives (Sonnet misreading a reinforcing instruction as a
contradiction). That's the memory the user was recalling. Nothing has been reviewed since. The queue
grew from those first 2 entries to 196 over the following six days (oldest entry 2026-07-16, newest
2026-07-18 — consistent with the Haiku correction gate's steady ~4.5% correction rate feeding the
contradiction checker daily) with zero manual triage in between. The 2026-07-14 "Lever #5" nightly
notification (`notify_pending_contradictions_backlog()`) *did* fire correctly — confirmed in
`launchd-stderr.log`: `"Notified: 130 pending contradictions >= threshold"` on 2026-07-18 — but a
once-daily macOS notification is easy to dismiss without connecting it to "run
`review-contradictions.py`," which is exactly what happened here.

**Bug found while investigating (not yet fixed, logged for follow-up)**: every one of the 196 entries
had `"project": null`. `pending_queue.append_pending()` accepts a `project` parameter, but neither
production call site (`contradiction_resolution.resolve()`, called from both `nightly-learn.py` and
`cocoindex-flows.py`) ever passes one — `resolve()`'s signature is `(bank_id, statement)`, and
`bank_id` is always the literal constant `BANK_ID = "cursor-memory"` (the one shared bank both
projects write corrections into), not a per-project value. This is why `report.py`'s
`count_pending_contradictions()` shows the identical "196 unresolved" under all three project
sections (kubernaut/dcm/engram) instead of a per-project breakdown — there's no project signal on
the entries for it to filter by. Not fixed here; would need threading the source transcript's
resolved project (same `transcript_id → project` mapping `purge-out-of-scope-memories.py` already
builds) through `retain_windows()`/`process_transcript()` into `resolve()` into `append_pending()`.

**Decision: given the size of the backlog and the specific known false-positive pattern (reinforcing
instructions misread as contradictions) plus zero confidence the rest are clean, dropped the entire
queue rather than manually triage 196 one at a time.** Backed up to
`~/.hindsight/logs/contradictions-pending.jsonl.bak-20260719-001133` before clearing (all 196
entries preserved on disk, not deleted, in case any turn out to have been worth acting on — same
non-destructive posture as `purge-out-of-scope-memories.py`'s dry-run-first pattern). Regenerated
`docs/PENDING_CONTRADICTIONS.md`/`docs/DASHBOARD.md` via `generate-dashboard.py`, both now correctly
show 0 pending. No production code changed — the underlying contradiction-check pipeline is
untouched and will start queuing fresh entries on the next correction-tagged retain.

**Lesson**: a passive, once-daily OS notification is not a substitute for either (a) actually
clearing the queue on a cadence, or (b) surfacing the backlog somewhere it's checked as part of
existing routine (e.g. the status-update report itself, which is how this was actually caught).
Consider lowering friction on review — 196 one-at-a-time interactive prompts is itself a reason the
queue never gets worked down; a batch/triage-by-category mode (similar to the Haiku false-positive
sampling done on 2026-07-09) would scale better than the current one-entry-at-a-time CLI.

## 2026-07-19: Fixed the `project: null` Bug — Pending/Auto-Resolved Contradictions Now Tagged Per-Project

**Fix for the bug logged above.** Root cause was exactly as diagnosed: `contradiction_resolution.resolve(bank_id,
statement)` never received or forwarded a project value, so `pending_queue.append_pending()`'s
`project` parameter always defaulted to `None` at both production call sites
(`nightly-learn.py`'s `retain_windows()`, `cocoindex-flows.py`'s `process_transcript()`).

**Fix**: threaded project resolution through the whole call chain instead of just adding a field
that still nothing would populate:
- `project_scope.py`: replaced the bare `ALLOWED_WORKSPACE_PREFIXES` list with a
  `PROJECT_LABEL_BY_PREFIX` dict (prefix → `"kubernaut"`/`"dcm"`/`"engram"`), with
  `ALLOWED_WORKSPACE_PREFIXES` now derived from its keys so the two concerns ("is this workspace in
  scope" and "which project does it map to") can't drift apart again. Added
  `resolve_project_label(project_dir_name)`.
- `contradiction_resolution.resolve()` gained a `project: str | None = None` parameter, forwarded to
  both `pending_queue.append_pending(..., project=project)` and `log_auto_resolved(..., project=project)`
  (the auto-resolved log had the identical gap — no `project` key at all — even though it wasn't
  called out in the bug report; fixed for the same reason while already threading the value through).
- `nightly-learn.py`: added `project_for_transcript_path(path)` (maps a transcript's
  `~/.cursor/projects/<workspace_dir>/...` path back to a label via `project_scope`), called at both
  `retain_windows_deduped()` call sites in `run_hourly()`/`run_nightly()` and threaded through
  `retain_windows_deduped()` → `retain_windows()` → `resolve()`.
- `cocoindex-flows.py`: `process_transcript()` derives the same label from
  `file.file_path.path.resolve()` relative to `ENGRAM_TRANSCRIPTS_DIR`, using the same string-prefix
  pattern already used elsewhere in that file (not `Path.relative_to()`, since `.resolve()` vs. the
  raw `.path` can disagree across symlinks).
- `report.py`'s `count_pending_contradictions()` gained a `project` filter parameter (entries with
  `project=None` — i.e. anything written before this fix, or any future entry whose transcript path
  couldn't be resolved — are excluded from every project-scoped count; there's no safe default to
  backward-compat them to, unlike the effectiveness-log project-tagging fix on 2026-07-09, since a
  pending contradiction's project can't be inferred from anything else in the entry). Also moved the
  hardcoded path into a proper `PENDING_CONTRADICTIONS_LOG` module constant for testability, matching
  `MCP_CALLS_LOG`/`EFFECTIVENESS_LOG`/etc. `format_report()` now takes the count as a parameter
  instead of computing it unscoped internally, so each project's report section shows its own count.
- `generate-dashboard.py`'s `docs/PENDING_CONTRADICTIONS.md` generator now shows a per-project
  breakdown line and tags each individual pending entry with its project — previously it only ever
  had `None`/`"?"` to display, since the underlying field was always null.

**Validated**: added regression tests at every layer of the chain (`resolve()` forwards `project` to
both the queue and the auto-resolved log; `project_for_transcript_path()` resolves kubernaut/dcm/
out-of-scope/outside-root paths correctly; `process_transcript()` derives and forwards the same
label; `count_pending_contradictions()` filters correctly and excludes null-project legacy entries).
193 tests pass (up from 169 pre-fix). Ran `report.py --days 1` and `generate-dashboard.py` against
live data post-fix — both completed without error; queue is currently empty (see prior entry) so the
per-project breakdown will only be visible once fresh entries accumulate under the fixed code path.

**Not done**: did not backfill a `project` onto the 196 entries backed up to
`contradictions-pending.jsonl.bak-20260719-001133` — they were already judged not worth the
per-entry review effort (see prior entry's decision to drop rather than triage), and this fix only
prevents the bug from recurring on new entries, not retroactively.

## 2026-07-17: Live In-Loop Write Decision — Design De-Risked via Spikes, NOT Implemented, Review Checklist Below

**Origin**: this idea came from comparing Engram against
[`Gentleman-Programming/engram`](https://github.com/Gentleman-Programming/engram),
a different project that happens to share the same name. That project has the
*agent* explicitly call a `mem_save` tool to curate what gets stored, with zero
async/inferred detection and zero LLM cost at write time. This entry explores
borrowing its live, in-loop signal — without adopting its trust model, since
this project's threat model (avoiding agent-hallucinated writes) requires the
extra verification layers below that `mem_save` doesn't need.

**Status: design + validation only. Zero production code changed.** `git status`
at end of this work shows exactly three new untracked files under `spike/`
(`evidence_span_matching_spike.py`, `groundedness_check_spike.py`,
`cocoindex_watch_latency_spike.py`) — `cocoindex-flows.py`,
`contradiction_resolution.py`, `correction_gate.py`, and `nightly-learn.py` all
have zero diff. No new MCP tool, no flag-queue consumer, no provisional-status
tagging exist. This entry exists so a future review knows exactly what was and
wasn't de-risked, and what to check before trusting this to gate real writes.

**The idea**: today, correction detection is entirely async/inferred — regex
or Haiku scanning transcript text after the fact (`correction_gate.py`,
`classify_correction`), with a measured recall of 0.80-0.93 (see 2026-07-14
entry below). The proposal is to give the *acting agent* a live, in-loop
signal: it flags **when** something notable happened (a correction, a
decision), and the existing pipeline still decides **what** gets written —
the agent's own flag is never trusted to write to `cursor-memory` directly.

**What was actually validated (2 spikes, both clean but small-sample)**:
- `spike/evidence_span_matching_spike.py` — verbatim/whitespace-normalized
  substring matching of an agent-cited `evidence_span` against real transcript
  text. 7/7 cases passed (verbatim, trailing-space, multiline-collapsed,
  paraphrase-rejected, fabrication-rejected, cross-message-boundary-rejected),
  zero false positives. This is the mechanical check that would block an agent
  from citing evidence that doesn't actually appear in the transcript.
- `spike/groundedness_check_spike.py` — for the specific gap where a claim
  doesn't contradict any existing memory (so `contradiction_resolution.resolve()`
  would return `action="retain"` with zero scrutiny), can Sonnet independently
  judge whether the claim is *supported* by its own cited evidence? 8/8 cases
  passed, including catching all 5/5 fabrication cases with zero false alarms
  on the 3 faithful ones.
- Both test sets were hand-picked and small (7, 8 cases) — clean results here
  are a good sign, not proof against adversarial or naturally-occurring edge
  cases at real volume.

**What was tested operationally (real infra, not simulated)**:
- Loaded both `io.vectorize.cocoindex.service` (shared kubernaut/dcm/engram
  pipeline) and the never-before-deployed `io.vectorize.cocoindex.engram`
  live, end-to-end, against real data — confirmed `process_transcript` genuinely
  parses real transcripts, flags `[CORRECTION]`/`[INSTRUCTION]` windows, and
  calls `hindsight_retain()` successfully (e.g. `transcript-56dfb74e-...-w0`
  landed in `cursor-memory` for real).
- A synthetic `[CORRECTION]`-shaped test file went **unprocessed for the full
  ~13 minutes observed** — as a new file in a new subdirectory, as a new file
  in an already-known directory, and as a modification to a known file, all
  three untouched while pre-existing (2-day-old) files got reprocessed. Looked
  at first like the live watcher might be broken.
- Followed up with `spike/cocoindex_watch_latency_spike.py` — the same
  `localfs.walk_dir(recursive=True, live=True)` + `PatternFilePathMatcher` +
  `coco.mount_each` primitives, pointed at a brand-new empty temp directory
  (zero backlog). All three cases (new subdir, new file in known dir,
  modification of known file) detected in **0.01-0.02s**. **Conclusion: the
  `watchdog`/FSEvents-backed live watcher is not broken — the ~13 minute delay
  was the initial backlog scan** (accumulated during a deliberate multi-day
  pause) queuing ahead of genuinely new events, not a detection failure. Real
  implication: Tier 2's "near-synchronous" property holds in steady state but
  degrades hard on the first restart after any extended pause — resume it
  well before you need near-real-time behavior from it, not exactly when you
  need it.

**Confidence, decomposed (not a single number)**:
- Design soundness if actually built: **~65%**, up from ~45-60% pre-spike.
  Both riskiest/most-novel pieces validated clean; the operational blocker
  (Tier 2 disabled) is resolved.
- That it moves Engram's own stated goals (`docs/METRICS.md`'s Correction
  Rate, Reduction %, Rework %/Tokens — "the same mistake never happens
  twice"): **~55%**. Decomposed:
  - Closing the current classifier's 7-20% recall gap (real misses = real
    repeat mistakes, since Engram literally doesn't know about them): **~75%**
    confidence this helps, and the most defensible direct link to the goal.
  - Latency reduction (catching corrections near-real-time vs. waiting for
    the nightly batch, benefiting *concurrent* sessions on the same project):
    **~60%**, contingent on Tier 2 staying warm operationally (see above).
  - Precision/pollution risk — a fabricated or misattributed "correction"
    slipping through is worse than a missed one, since a wrongly-recalled
    lesson actively creates new rework: **~30%** chance of net-negative here,
    only partially mitigated by the two spikes' small samples.
  - Measurement wrinkle: **Correction Rate is computed by running a
    classifier over transcripts** — improving recall will likely make the
    *measured* corrections/session go **up** at first (catching what was
    previously invisible), which could misread as a regression. The metric
    that actually matters here is **Reduction %** and repeat-mistake rate
    over several weeks, not the immediate corrections/session count.

**Recommended path, if/when this gets built** (methodology-driven, not a
blanket "spike everything" or "just ship it"):
1. New MCP tool for the agent to flag "when," and wiring it into a Tier 2
   consumer calling the already-validated pieces — implement directly, no
   spike needed. This is assembling parts already proven individually
   (evidence-span match, `classify_correction`, `contradiction_resolution`,
   groundedness check), not an unknown.
2. Run the actual gating decision in **shadow/observe-only mode first** —
   this repo already has the right precedent in `prefilter-shadow-trial.py`
   (logs verdicts, "gates nothing for real"). Do the same here before trusting
   it to write to `cursor-memory`.
3. One concrete, cheap, unverified assumption to check **before** writing the
   provisional-status/promotion logic: `retain`'s MCP schema supports `tags`
   on write and `recall`'s schema has `tags`/`tags_match`/`tag_groups` on
   read, but nothing in this codebase has ever exercised *tag exclusion*
   (`grep`-verified: zero hits for exclusion-style tag usage anywhere in
   `*.py`). The "hide provisional items from recall until promoted" mechanism
   assumes exclusion works the way the design wants — verify this against the
   real Hindsight API before building promotion/cooldown around it, not after.
4. Rate/plausibility guard against agent over-flagging — not designed in
   detail yet; better tuned from real shadow-mode data than spiked in the
   abstract.

**Decision (same day, after the above was written): deferred, not built.** Re-checked
`correction_gate.py`'s actual default before deciding — `ENGRAM_CORRECTION_DETECTOR`
defaults to `"haiku"`, not regex, measured at ~0.97 F1 against 630 Haiku-confirmed
corrections in the 2026-07-08 shadow trial (see that date's entry). That's a stronger
baseline than this entry's earlier confidence section implied by citing 0.80 recall
as if it were today's live gap — 0.80 was the regex *fallback*'s number, not what's
actually running. With Haiku already default, the ceiling a new agent-self-flag
MCP tool could realistically close is roughly the last ~3%, not ~20%, for a
mechanism whose entire safety net (steps 2-8 above) is still unbuilt and whose
rollout would mean editing AGENTS.md/cursor rules across three projects
(kubernaut, dcm, engram each have their own `.mdc` file), not just this one.
Risk/reward doesn't justify building it right now.

**Chosen path: lean on the resumed Tier 2 pipeline as-is.** No new MCP tool, no
AGENTS.md/cursor-rules changes, on any project. The existing Haiku-classifier
path, now running near-real-time instead of nightly-only (see the watch-latency
finding above), already captures most of the achievable benefit with zero new
hallucination/fabrication surface.

**Explicit revisit trigger** (so this isn't just "someday, maybe"): come back to
the MCP-tool plan only if, after several weeks of Tier 2 running warm, the real
`Correction Rate`/`Reduction %` trend data (`docs/METRICS.md`) shows the residual
gap is actually large enough in practice to justify the added complexity and
risk — not before there's real trend data to justify it.

**Review checklist for whoever picks this back up**:
- [ ] Is the MCP tool + flag-queue actually built, or still just this design?
- [ ] Was the tag-exclusion assumption (point 3 above) verified against the
      real Hindsight API? What was the result?
- [ ] Has it run in shadow/observe-only mode, and for how long, before any
      gating was enabled for real?
- [ ] What's the false-positive rate on real (not hand-picked) agent
      self-flags, once there's real volume?
- [ ] Did `Correction Rate` go up (expected, not a red flag on its own) while
      `Reduction %` / observed repeat-mistake rate over weeks trended the
      right way (the actual bar this needs to clear)?
- [ ] Is Tier 2 (`io.vectorize.cocoindex.service` / `.engram`) still loaded
      and warm, or did it get paused again and accumulate backlog?

## 2026-07-15: Engram Onboarded Into Its Own Hindsight+CocoIndex Project, kubernaut-operator/console Get Tag-Scoped Recall

**Why now**: Engram itself had no `hindsight-docs`/`cocoindex-code` presence — none of this repo's
own docs or Python source were recallable/searchable the way kubernaut's and dcm's are, despite
this being the project doing the most active development at the time. Separately, the user asked
whether `kubernaut-operator`/`kubernaut-console` (sub-repos of the `kubernaut` org, already
ingested into the shared `kubernaut-docs` bank / `code_embeddings` table) should get their own
dedicated banks, with cross-repo query access back to core `kubernaut` for triage. Researching that
split first (before implementing Engram onboarding) turned up enough live data to change the
recommended design.

**Part B decision, made before Part A implementation — tag-scoped recall instead of a bank
split.** Direct measurement of the shared bank/table showed a clean, already-correct partition:
pgvector `code_embeddings` has 719 `kubernaut-operator/` rows, 894 `kubernaut-console/` rows, 19,222
core `kubernaut/` rows; the `kubernaut-docs` Hindsight bank has 341 docs tagged
`kubernaut-operator`, 255 tagged `kubernaut-console`, out of 38,777 total — ingestion was already
tagging by repo correctly, the only thing missing was a way to *query* by that tag. A full bank
split would have required scripting explicit deletes for both sinks (pgvector's `declare_row`/
`declare_target_state` likely auto-GCs orphaned rows when a `mount_each` block changes, but this
is unverified for a live production table; a Hindsight-bank retain is a plain HTTP POST inside a
memoized CocoIndex function, so CocoIndex has zero visibility into it and would **never**
auto-delete anything left behind) — real migration risk for a benefit (query isolation) that
Hindsight's/CocoIndex's own `tags`/`repo`-filter parameters already deliver without moving any
data. Implemented instead:
- Two new tag-scoped mental models in the existing `kubernaut-docs` bank via `create_mental_model`'s
  `tags` field: `operator-architecture` (`tags: ["kubernaut-operator"]`), `console-architecture`
  (`tags: ["kubernaut-console"]`) — see `create-mental-models.py`. Both added to
  `nightly-learn.py`'s `PROJECT_CONFIGS["kubernaut"]["mental_models"]["kubernaut-docs"]` tuple so
  they refresh nightly alongside the existing 3 (`ka-architecture`, `af-pipeline`,
  `platform-topology`), bringing that bank's total to 5.
- An optional `repo` parameter on `cocoindex-search.py`'s `search_code()` / `cocoindex_search` MCP
  tool: when given, adds `WHERE filepath LIKE '<repo>/%'` to both the dense and BM25 legs of the
  hybrid search before RRF fusion; omitted (the default, used by core `kubernaut` work), searches
  everything unchanged.
- Hand-authored, project-specific `.cursor/rules/hindsight-memory.mdc` for both repos (via new
  `cursor/projects/operator.vars`/`console.vars` + `cursor/operator-hindsight-memory.mdc`/
  `console-hindsight-memory.mdc`), replacing what had been byte-identical stale clones of the
  global template with no project customization and no `.vars` source at all. New guidance:
  default to `tags: ["kubernaut-operator"]`-scoped recall and `repo: "kubernaut-operator"`-scoped
  code search for own-repo work; drop both filters explicitly for cross-repo/upstream triage
  (the exact "quickly triage failures against upstream kubernaut" use case that prompted the
  research). Console's copy drops the Go/`gopls` section (TypeScript/React, not Go) the same way
  `engram`'s already does; both copies also picked up the `max_tokens` right-sizing section
  (lever #3, 2026-07-14 entry below) that their stale pre-existing copies were missing entirely.

**Part A — Engram onboarded as its own project**, following the same pattern as kubernaut/dcm but
with two variants (now documented in `docs/NEW_PROJECT_SETUP.md`'s new "Variants" section so the
next onboarding doesn't have to rediscover them): no `engram-issues` bank/app at all (this repo has
zero GitHub issues; bugs and decisions live in this file instead), and the new `engram-docs` bank's
2 mental models (`engram-architecture`, `engram-operations`) start as empty shells with no refresh
until real content exists. New `engram-cocoindex-flows.py` (`docs_app`: `docs/*.md` →
`engram-docs`; `code_app`: `*.py` → `cocoindex.engram_code_embeddings`, excluding
`__pycache__`/`.pytest_cache`/`.git`/`venv`/`node_modules`) and `engram-cocoindex-search.py`
(hybrid dense+BM25+RRF over the new table, MCP tool `engram_code_search`), both symlinked into
`~/.hindsight/` matching the existing pattern. New `launchd/io.vectorize.cocoindex.engram.plist`
created but **not loaded** — this repo's own ingestion stays paused alongside the other 6 already-
paused jobs from the mid-session scale-down, consistent with not starting new background load
during a declared pause.

**Three real bugs found while building Part A, none hypothetical — all now regression-tested:**

1. **`ENGRAM_CONSOLE_DIR` in `cocoindex-flows.py` still pointed at the renamed-away
   `kubernaut-demo-console` path** (should be `kubernaut-console`) — a directly blocking bug found
   at plan time via `ls`, not by a user report. Left unfixed, the new console-tag-scoped work would
   have kept relying on stale rows from a backfill that ran before the rename (the 894/255
   console rows/docs currently in the shared sinks all predate it), and any future console
   backfill would find zero files. Fixed in both the repo's `cocoindex-flows.py` and the deployed
   `~/Library/LaunchAgents/io.vectorize.cocoindex.service.plist.disabled` (which had drifted to the
   same stale value independently of the repo copy) — also brought the repo's own template plist
   (`launchd/io.vectorize.cocoindex.service.plist`) up to date with `ENGRAM_OPERATOR_DIR`/
   `ENGRAM_CONSOLE_DIR`/`ENGRAM_SCENARIOS_DIR`/`ENGRAM_ISSUES_REPOS`, none of which the checked-in
   template previously declared at all despite the live deployment needing them.
2. **`nightly-learn.py`'s `analyze_mcp_effectiveness()` had `RECALL_BANKS`/`CODE_BANK` hardcoded to
   kubernaut's own MCP server names** (`"hindsight"`, `"hindsight-docs"`, `"hindsight-issues"`,
   `"cocoindex-code"`) at module scope, used for every project regardless of the `project` argument.
   DCM's actual server names (`dcm-docs`, `dcm-issues`, `dcm-code`) never matched that hardcoded
   set, so `banks_recalled` filtering and the `with_cocoindex` exploration-efficiency bucket were
   silently zeroing out DCM's cocoindex usage in every run to date — DCM sessions that *did* use
   `cocoindex_search` were being bucketed as if they hadn't. Fixed by deriving both from
   `PROJECT_CONFIGS[project]["recall_banks"]`/`["code_bank"]` instead (new keys added to every
   project's config). Regression-tested with a cross-check that specifically proves the fix is a
   real per-project derivation and not just a widened accept-everything set:
   `test_nightly_learn.py::TestRecallBanksPerProject::test_dcm_server_name_not_counted_under_kubernaut_project`
   asserts a `dcm-code` recall analyzed under `project="kubernaut"` does **not** count, alongside
   the positive case that it *does* count under `project="dcm"`.
3. **`report.py`'s `collect_ingestion_coverage()` queried GitHub issues/PRs for a single hardcoded
   `jordigilh/kubernaut` repo**, regardless of which project's report was being generated — so
   DCM's (and every other project's) issues/PRs total was always the kubernaut count, not its own
   (in practice this manifested as DCM always showing zero real coverage relative to its own repos,
   since the loop never queried any of them). Fixed by adding `issues_repos` lists to
   `PROJECT_CONFIGS` (kubernaut: 4 repos including `-operator`/`-console`/`-demo-scenarios`; dcm: 12
   repos) in both `nightly-learn.py` and `report.py`, and rewriting the loop to sum across
   `PROJECT_CONFIGS[project]["issues_repos"]` when a project is given, or every configured
   project's repos combined when it isn't. A project with no `issues_repos` key at all (engram)
   simply contributes nothing to the total rather than erroring or defaulting to kubernaut's repo —
   verified directly in `test_report.py::TestCollectIngestionCoverageProjectScoping`.

**Test-infra-only bug, not a production bug (same class as the 2026-07-13 `pg_pool` collision
below it in this file)**: `engram-cocoindex-flows.py` initially reused the exact same
`coco.ContextKey("pg_pool")` name `cocoindex-flows.py` already registers for its own Postgres
pool. CocoIndex registers `ContextKey`s process-globally and raises `ValueError` on a same-name
second registration — harmless in real deployment (each flow file is its own long-running
`launchd` process), but fatal for the pytest suite, which loads both hyphenated files as modules
in one process via `conftest.py`'s `load_hyphenated_module()`. Renamed to
`"engram_repo_pg_pool"` before writing any tests against the new file, confirmed both modules now
load together without error, and documented the naming requirement directly next to the
`ContextKey()` call plus in `docs/NEW_PROJECT_SETUP.md`'s CocoIndex-flows step, so the next
per-project flow file doesn't reintroduce it.

**Tracked but not fixed — `create-mental-models.py`'s `refresh_after_consolidation` drift.**
While auditing the 3 existing `kubernaut-docs` models (`ka-architecture`, `af-pipeline`,
`platform-topology`) for the operator/console tag-scoping work, found the source file declares
`"refresh_after_consolidation": False` for all three, but the *live* Hindsight API currently
reports `True` for them — meaning someone (or something) changed the live trigger config directly
via the API at some point without updating the source-of-truth Python file, and the two have been
silently diverged since. Deliberately **not fixed here**: it's unrelated to this session's actual
work, and it isn't obvious which direction is correct (the live `True` could be an intentional
manual tune that the source just never caught up to, or an accidental change that should be
reverted) — logged here as a flagged gap rather than guessed at. Next time `create-mental-models.py`
is touched, diff its `MENTAL_MODELS` trigger config against a live `GET
/v1/default/banks/kubernaut-docs/mental-models` response before assuming the source file is
authoritative.

**Test suite growth**: 136 → 168 tests. New file `test_engram_cocoindex_flows.py` (10 tests: module
loads without the `ContextKey` collision, `_split_text` chunking, `process_doc_file`'s path →
document_id/tags/section derivation, `hindsight_retain`'s retry contract — mirrors the existing
`test_cocoindex_flows.py` coverage ceiling for the analogous kubernaut file). New classes in
`test_nightly_learn.py` (`TestRecallBanksPerProject`, 5 tests; `TestProjectConfigsEngram`, 3 tests)
and `test_report.py` (`TestProjectConfigsEngram`, 3 tests; `TestCollectIngestionCoverageProjectScoping`,
5 tests) pin the three real bugs above so a future refactor of either file's per-project scoping
can't silently regress them. New `TestRulePairsRealShape` in `test_check_rule_sync.py` pins the
real (non-monkeypatched) `RULE_PAIRS` dict — the existing `TestMain` class only ever exercises fake
pairs, so it couldn't have caught a future edit accidentally dropping the `engram`/`operator`/
`console` pairs added in this session.

**Rollback instructions**: Part A is fully additive — delete `engram-cocoindex-flows.py`,
`engram-cocoindex-search.py`, their `~/.hindsight/` symlinks, the `engram-docs` bank, and the
`"engram"` `PROJECT_CONFIGS` entries in `nightly-learn.py`/`report.py` to fully remove; the new
`launchd` plist was never loaded, so there's no running process to stop. Part B: delete the two new
tag-scoped mental models via the Hindsight API, revert `cocoindex-search.py`'s `repo` parameter
(optional, backward-compatible — omitting it is already the unchanged default), and restore
operator/console's `.cursor/rules/hindsight-memory.mdc` from git history if the tag-scoped
guidance turns out not to be followed correctly in practice. The three bug fixes (console path,
`RECALL_BANKS`/`CODE_BANK`, `issues_repos`-based coverage totals) should **not** be rolled back
independently of the features above — they're correctness fixes for pre-existing multi-project
support, not new-project-specific behavior.

## 2026-07-14: Six Input-Token-Reduction Levers — Confidence-Gated Triage, Three Spikes, All Six Shipped

**Why now**: Reading Gergely Orosz's ["The Pulse: Interesting AI coding stats from
Cursor"](https://newsletter.pragmaticengineer.com/p/the-pulse-interesting-ai-coding-stats) (90% of
Cursor's token volume is input, not output) prompted a review of how Engram's `recall`
(Hindsight) and `cocoindex_search` (CocoIndex) could push further on the same problem using only
what's already deployed. The resulting 6-lever proposal was first triaged for factual accuracy
against live data (two levers — MCP server "fragmentation" and rule-deployment parity — turned out
to be based on wrong assumptions and were corrected before implementation), then confidence-scored
per this project's mandatory gate before any code was written: **1 (95%) and 5 (95%) and 6 (90%)
cleared the ≥90% bar immediately; 2 (55%), 3 (60%), and 4 (70%) did not** and required a spike each
before implementation, per the same gate's own escalation path ("run spikes to close the gap, don't
implement below threshold").

**Lever #1 — surface `context_loading_tokens` (95% confidence, implemented as-is).**
`nightly-learn.py`'s `analyze_mcp_effectiveness()` already computed
`context_loading_tokens` (preamble chars before first productive action, ÷4) into every daily JSON
log, but `report.py`/`generate-dashboard.py` never read it. Added `avg_context_loading_tokens` to
`recall_session_stats` (both the per-run dict in `nightly-learn.py` and the multi-day weighted
aggregation in `report.py`), a new "Avg context-loading tokens" line in the CLI report, a $-cost
line using the same Sonnet-input-rate formula already applied to "tokens wasted on corrections",
and a new row in `generate-dashboard.py`'s trend table. Two new regression tests pin the exact
message-loop boundary (`test_context_loading_tokens_stops_at_first_productive_action`,
`test_context_loading_tokens_excludes_the_productive_turn_itself`) so a future refactor can't
silently change what "before first productive action" means.

**Lever #5 — standing-cadence nudge for the pending-contradictions backlog (95% confidence,
implemented as-is).** The backlog was already passively surfaced (dashboard warning banner,
`docs/PENDING_CONTRADICTIONS.md`), but clearing it depended entirely on someone remembering to
check. New `notify_pending_contradictions_backlog()` in `nightly-learn.py` fires one macOS
notification (`osascript display notification`) per calendar day once the queue is at or above
`ENGRAM_CONTRADICTION_NOTIFY_THRESHOLD` (default 10), tracked via a `last-contradiction-notify.txt`
state file so it stays idempotent across `nightly-learn.py`'s two separate per-project launchd
invocations (kubernaut, dcm) each night — without that guard the same global backlog would
double-notify every night.

**Lever #6 — diff the deployed rule against the repo's canonical copy (90% confidence, implemented
as-is; caught real drift on first run).** New `check-rule-sync.py` diffs
`~/.cursor/rules/hindsight-memory.mdc` (deployed) against `cursor/hindsight-memory.mdc` (this repo's
canonical copy) and supports `--fix` to copy canonical → deployed. First run found the two actually
had drifted — a cosmetic line-wrap difference in the `cocoindex_search` paragraph, not a content
change — confirming the tool catches real (if benign, this time) drift rather than being a
speculative safeguard. `docs/INSTALL.md`'s rule-install step now points at it.

**Lever #4 — normalize MCP server names before aggregating hit-rate stats (70% → ~90% after
spike).** Sampled the live `~/.hindsight/logs/mcp-calls.jsonl` (857 calls, 21 distinct raw server
names) before writing any normalization regex. Confirmed the core hypothesis: Cursor prepends a
project-workspace-derived prefix (`kubernaut-`, `kubernaut-v1.6-`, `project-0-kubernaut-`,
`enhancements-`) and sometimes an `::mcpScope:profile:...:project:...:cfg:...` suffix to server
names at call time, fragmenting one correctly bank-scoped tool across many near-duplicate rows —
all such variants for `hindsight-docs`/`hindsight-issues`/`cocoindex-code` were ≥96% hit rate,
confirming they're cosmetic renames of a working tool, safe to merge. **But the spike also found a
real anomaly the original proposal didn't anticipate**: every `user-*` prefixed call (6 total,
across `user-cocoindex-code`, `user-hindsight-docs`, `user-hindsight-issues`) was a 100% miss.
Blindly normalizing everything would have diluted that signal into the healthy majority instead of
keeping it visible. `report.py`'s new `normalize_server_name()` strips the project-prefix/mcpScope
patterns but explicitly excludes `user-*`, so those rows stay separately visible. Verified against
the live log: 21 raw rows → 11 normalized rows, with the `user-*` anomaly rows intact and unchanged.
The root cause of the `user-*` binding issue itself is still open — flagged for follow-up
investigation, not fixed here (fixing an unconfirmed binding bug is a different, riskier task than
an observability fix).

**Lever #3 — right-size recall's payload for narrow queries (60% → ~90% after spike, and the
mechanism changed).** The original proposal targeted the `budget` parameter ("reserve high for the
methodology gate, use lower budgets for narrow follow-ups"). Direct measurement disproved this:
`budget: "low"` vs. `budget: "high"` on the same query returned 57.3KB vs. 58.3KB (and on a second,
unrelated query, 57.9KB vs. a similar-sized high-budget response) — no measurable difference,
within noise, in the wrong direction if anything. `max_tokens` (a separate, independent parameter,
default 4096) is the actual lever: `max_tokens: 500` returned 8 results (~3KB), `max_tokens: 1000`
returned 14 results (~6.4KB), and the default 4096 returned 66-68 results (~57-60KB) on the same
query — a real, substantial, reproducible effect. Rewrote `cursor/hindsight-memory.mdc`'s guidance
accordingly (new "Right-size the payload for narrow queries" section): omit `max_tokens` for
broad/first-turn recalls, pass `800`-`1500` for narrow follow-ups, and don't bother with `budget`
for this purpose. Redeployed via `check-rule-sync.py --fix` (lever #6's own tool, used for real for
the first time here).

**Lever #2 — refresh mental models on topic-shift, not just nightly (55% → ~85% after spike).** The
blocking gap was that "topic-shift" wasn't a defined signal anywhere, and each refresh is a real
Sonnet resynthesis call (confirmed 8-14KB of output per model against the live `cursor-memory`
bank's four models) — an undebounced trigger risked a real cost regression. Investigated Hindsight's
`pending_consolidation` bank-stats field as a possible ready-made signal; rejected it, because it
equaled `total_nodes` (2343 = 2343) with no visible per-refresh delta behavior confirmable from the
API alone — using an opaque, unverified signal would have been worse than not spiking at all.
Instead designed a fully self-controlled counter: new
`maybe_refresh_mental_models_on_topic_shift()` in `nightly-learn.py` increments a per-bank
`count_since_refresh` (persisted to `model-refresh-state.json`) by `windows_retained` after every
hourly retain, and triggers an immediate refresh of `cursor-memory`'s four models once the counter
reaches `ENGRAM_TOPIC_SHIFT_REFRESH_THRESHOLD` (default 5) — gated by a second, independent
`ENGRAM_TOPIC_SHIFT_REFRESH_MIN_INTERVAL_HOURS` (default 4) debounce so a burst of corrections can't
trigger repeated resynthesis calls. `run_nightly()`'s existing unconditional refresh now also resets
the counter for every topic-shift-tracked bank it covers, so the two mechanisms don't fight (nightly
refreshing for free right after an hourly topic-shift trigger already covered the same material).
Only `cursor-memory` is wired in (`TOPIC_SHIFT_MODELS`) since it's the only bank `run_hourly()`
writes to directly — `kubernaut-docs`/`-issues` and `dcm-docs`/`-issues` are populated by separate
ingestion pipelines and still refresh only nightly.

**Test suite growth**: 108 → 136 tests (3 new files — `test_check_rule_sync.py`, `test_report.py` —
plus additions to `test_nightly_learn.py`), all passing, all offline (every `osascript`/`api_post`/
Hindsight call mocked via `monkeypatch`). New regression guards worth calling out specifically:
`test_report.py::test_regression_user_prefix_is_never_stripped` and
`test_regression_user_scoped_misses_stay_visible_not_diluted` (lever #4's anomaly-preservation
behavior), and `test_nightly_learn.py::test_regression_debounced_within_min_interval_even_above_threshold`
(lever #2's cost-containment behavior) — both pin behavior that a "just make it simpler" refactor
could easily and silently break.

**Rollback instructions**: levers #1/#5/#6 are additive and side-effect-free to revert (delete the
new lines/files; #5's notification and #6's `--fix` only ever touch their own state
file/`~/.cursor/rules/hindsight-memory.mdc`). Lever #4: revert `report.py`'s `by_server`/`by_day`
keys to the raw `entry.get("server", ...)` value to restore pre-normalization behavior. Lever #3:
revert `cursor/hindsight-memory.mdc`'s new section and re-run `check-rule-sync.py --fix`. Lever #2:
set `ENGRAM_TOPIC_SHIFT_REFRESH_THRESHOLD` to a very high number (e.g. `999999`) to effectively
disable without removing code, or delete `~/.hindsight/model-refresh-state.json` and the call site
in `run_hourly()` to fully remove.

## 2026-07-12: Shipped the Haiku Correction Gate and Contradiction Check to Production (Haiku/Sonnet Only, No New Infra)

**Why now**: This traces back to comparing Engram against
[AutoMem](https://autolearnmem.github.io/), a framework where an LLM agent learns to manage its
own memory as a cognitive skill via two meta-LLM loops — one that tunes the agent's own scaffold
(prompts, action vocabulary, gating logic), one that trains a dedicated memory specialist.
Self-hosting a fine-tuned specialist model wasn't feasible given this environment's resources, but
AutoMem's "Loop #1" — an LLM judging/gating the agent's own actions instead of a fixed heuristic —
is the same category of change as replacing Engram's regex correction gate with a Haiku-judged one.
Auditing what was already built but never wired into production surfaced three validated
components sitting unused; AutoMem's results were the concrete reason to prioritize closing that
gap now rather than wait on a self-hosted specialist model.

**What changed** — three phases, all using only the existing Haiku/Sonnet Vertex AI setup:

**Phase 1 — Correction detection: regex → Haiku.** New shared module `correction_gate.py`
(imported by both `nightly-learn.py` and `cocoindex-flows.py`) replaces the
`CORRECTION_PATTERNS`/`STRUCTURAL_CORRECTION_PATTERNS` regex gate with `spike/classify.py`'s
`classify_correction()` (Haiku), disk-cached by `sha256(text)` since `cocoindex-flows.py`'s
`process_transcript` reprocesses a transcript's entire content on every change (no incremental
slicing there, unlike `nightly-learn.py`'s watermark system) — without the cache this would
re-classify every earlier message in a session with Haiku on every subsequent message. Also ports
the boilerplate-message filter found by the 2026-07-08 Prefilter Shadow Trial entry below (Cursor's
own injected subagent-completion/system-reminder templates, attributed to `role="user"`), which
that entry explicitly flagged as "worth reconsidering if either [production file] ever adopts
semantic classification" — that's happening now. `ENGRAM_CORRECTION_DETECTOR=haiku|regex` (default
`haiku`) is a one-line rollback; the old regex lists stay in the codebase, unused by default. The
Prefilter Shadow Trial's 14-day/3,873-message backfill measured Haiku at 16.3% correction rate vs.
the best regex candidate's 24.4% recall against Haiku's own verdicts — that gap is what this phase
closes.

**Phase 2 — Contradiction check wired in, three-tier resolution.** `spike/classify.py`'s
`check_contradiction()` (Sonnet) is now called from both production retain paths
(`nightly-learn.py`'s `retain_windows()`, `cocoindex-flows.py`'s `process_transcript()`) for every
`[CORRECTION]`-tagged window, via a new shared `contradiction_resolution.py`:
- No contradiction → retain as before.
- Contradicts, confidence ≥ `ENGRAM_CONTRADICTION_AUTO_THRESHOLD` (default `0.9`) → auto-resolve.
  Ships **shadow-first** (`ENGRAM_CONTRADICTION_AUTO_MODE=shadow` default): logs what it would
  delete/supersede to `contradictions-auto-resolved.jsonl` without actually deleting; flip to
  `live` only after reviewing real shadow output, the same bar this project applied before trusting
  `classify_correction` itself.
- Contradicts, below threshold → queued via `spike/pending_queue.py` for human review.
  `generate-dashboard.py` now also generates `docs/PENDING_CONTRADICTIONS.md` (full entry detail
  plus the auto-resolved rollup) alongside `docs/DASHBOARD.md`, so outliers surface automatically
  in the existing nightly/on-demand reporting flow instead of requiring a separate script anyone
  has to remember to run. Resolve with `python3 review-contradictions.py`.

Three real bugs were found and fixed while wiring this up, not just inferred:
- `spike/hindsight_client.py`'s `recall()` parsed a `"chunks"` key the live `hindsight-api` never
  populates (always `{}`) — the real shape is `{"results": [...]}`, matching
  `nightly-learn.py`'s own `measure_recall_quality()`. **This means `recall()` had never actually
  seen real memory content before this fix** — which puts the 2026-07-08 "Semantic Correction
  Detection Spike" entry's real-world sanity check ("0 false-positive contradictions") in the same
  category as the "zero corrections detected" measurement-artifact incident: a clean-looking
  number that may reflect broken plumbing rather than a real signal, not a validated result to
  keep relying on. Fixed to parse `results` and return `(document_id, text)` pairs; re-ran the
  same style of check against live `cursor-memory` content post-fix and got correct verdicts.
- `review-contradictions.py`'s "approve" path never actually deleted or invalidated the superseded
  memory — it only tagged the new one with `supersedes` metadata that nothing downstream read.
  Both the manual approve path and the new auto-resolve tier now call a real
  `DELETE /v1/default/banks/{bank}/documents/{document_id}` (mirroring `nightly-learn.py`'s
  `dedup_graph()` pattern) via `contradiction_resolution.delete_document()`.
- The "queued" tier's own `resolve()` docstring initially claimed "the caller retains the new
  statement in every case" — but `spike/pending_queue.py` (unmodified, pre-existing) explicitly
  documents queued entries as "withheld from `hindsight_retain()` pending human confirmation...
  never auto-retained," and `review-contradictions.py`'s `[r]eject` describes itself as "discard
  the new statement, keep the existing memory." Under the original wiring, `retain_windows()` /
  `process_transcript()` called retain unconditionally regardless of `resolve()`'s verdict, so a
  queued item was *already permanently retained* before a human ever saw it — `[r]eject` had
  nothing left to discard, and `[a]pprove` would have created a second, duplicate copy under a
  different `document_id`. Caught immediately after the first live run (see below) surfaced two
  real queued entries and both turned out to be false positives, which made the bug's *absence*
  of consequence in that specific case obvious but not the underlying asymmetry: a queued item
  where the *new* statement is the wrong one had no way to actually get removed. Fixed by adding
  `continue`/skip-retain in both call sites when `resolution.action == "queued"`; verified with
  monkeypatched `resolve()` returning each of the three actions and asserting retain is called
  exactly 0 (queued) or 1 (retain/auto_resolved) times.

`ContradictionResult` gained a `confidence` field (mirroring `ClassificationResult`'s existing one)
so the two tiers above have a real signal to threshold on. Spiked confidence separation before
trusting it: 0.85 on the classify.py suite's documented "blanket rule vs. narrow exception" hard
case vs. 0.95–0.99 on three clear-cut cases — real and directionally correct, but n=4 is too small
to trust for live deletes, hence the shadow-first rollout above. `ENGRAM_CONTRADICTION_CHECK=on|off`
(default `on`) is the whole-feature rollback switch.

**Phase 3 — Two new `report.py` metrics, pure surfacing of existing data**: empty-recall rate
(`1 - hit_rate` from `mcp-calls.jsonl` entries filtered to `tool == "recall"`) and writes-per-search
ratio (`sum(windows_retained)` from daily JSON logs ÷ recall-call count over the same window). No
new logging needed. `load_daily_logs()` gained a `log_suffix` parameter so this stays scoped
per-project (kubernaut vs. dcm), matching every other per-project metric in the file.

**Unplanned but necessary infra fix, found while implementing Phase 1**: `nightly-learn.py`'s
hourly/nightly `launchd` jobs invoke `/usr/bin/python3` — macOS **system** Python 3.9.6 — which has
never had any third-party dependency, since the script was pure-stdlib until now.
`correction_gate.py` calls `litellm` (via `spike/classify.py`), which only exists in
`~/.hindsight/venv` (the same venv CocoIndex already runs under). Confirmed by direct test that
`nightly-learn.py` runs correctly under the venv's Python (already 3.9-safe via
`from __future__ import annotations`), then repointed both `launchd/io.vectorize.hindsight.hourly.plist`
and `.../nightly.plist` at `~/.hindsight/venv/bin/python3`, regenerated and reloaded the live
installed jobs, and updated `docs/INSTALL.md` (including the missing `correction_gate.py` /
`spike/` symlink steps for `nightly-learn.py`, which previously only needed them documented for
CocoIndex). Without this, every hourly/nightly run would have crashed with
`ModuleNotFoundError: No module named 'litellm'` the first time it hit a correction-tagged message.

**What to verify once 24h+ of live uptime resumes** (see the 2026-07-08 "Correction Detection
Missed 100% of 'Not Following Methodology' Corrections" entry below — `corrections_detected` reading
`0` for three straight days despite frequent real corrections was itself the red flag that started
this whole investigation, and needs real uptime rather than a code review to confirm is actually
fixed): the
correction-detection rate should trend toward the shadow trial's observed 16.3%, not the regex
gate's far lower historical rate; `count_pending_contradictions()` in `report.py` /
`docs/PENDING_CONTRADICTIONS.md` should start showing real (rather than structurally-empty) data
once any live contradictions occur; and `contradictions-auto-resolved.jsonl` should accumulate
shadow-mode entries to review before ever flipping `ENGRAM_CONTRADICTION_AUTO_MODE` to `live`.

**Rollback instructions**: `ENGRAM_CORRECTION_DETECTOR=regex` reverts Phase 1 to the old regex gate
(and removes the venv-interpreter requirement, though there's no harm leaving it as-is either way).
`ENGRAM_CONTRADICTION_CHECK=off` disables Phase 2 entirely; `ENGRAM_CONTRADICTION_AUTO_MODE=shadow`
(the default) keeps the auto-resolve tier logging-only even with the feature on. Phase 3 is pure
reporting and has no failure mode beyond a missing/empty section if the underlying logs are absent.

**First live run, immediately after deploy**: manually triggered the hourly `launchd` job end to
end. It ran Haiku classification and Sonnet's contradiction check against real `cursor-memory`
content and queued 2 entries in `contradictions-pending.jsonl` for human review — the pipeline's
first real (non-shadow, non-spike) contradiction signal. Both were reviewed and confirmed **false
positives**: one was Sonnet reading an instruction ("stop using HAPI, it's deprecated") as if it
contradicted a prior "migrate off HAPI" memory rather than reinforcing it; the other similarly
misread a declarative instruction as conflicting rather than corroborating. Both were phrased as
flat statements rather than questions, which the user flagged as a real contributing factor —
Sonnet's `_CONTRADICTION_SYSTEM_PROMPT` (`spike/classify.py`) isn't yet tuned to distinguish
"restating/reinforcing an existing rule" from "contradicting" it, especially for imperative
sentences. Not fixed yet — logged here as a concrete prompt-tuning candidate rather than acted on
immediately, since n=2 is too small to safely tighten the prompt without risking false negatives
in the other direction. This same live run is what surfaced the third bug above.

## 2026-07-13: Project Scoping Fix for the Retain Pipeline + First Regression Test Suite

**Why now**: Two issues surfaced while reviewing the prior entry's first live run: (1) one of the two
queued contradictions traced back to a transcript from `koku` (an unrelated project, not
kubernaut/dcm/engram), which meant the shared `cursor-memory` bank was absorbing project-specific
technical decisions from every Cursor workspace on the machine, not just onboarded projects; and (2)
three real bugs (chunks-vs-results, missing delete-on-approve, queued-tier-retained-anyway, all
described in the entry above) had shipped to production in one session with zero automated coverage
catching any of them.

**Problem 1 — unscoped transcript ingestion.** Neither `nightly-learn.py`'s `run_hourly()`/
`run_nightly()` nor `cocoindex-flows.py`'s `transcript_app` had any project filter on the retain path
itself — `find_recent_transcripts()` was called with no `workspace_prefixes` argument (even though
that parameter already existed, used only for analytics scoping), and `PatternFilePathMatcher`'s
`included_patterns=["**/*.jsonl"]` matched every one of the ~270+ Cursor workspaces under
`~/.cursor/projects/`. Confirmed impact at plan time: 139 of 444 transcript-traceable `cursor-memory`
documents (31%) came from out-of-scope workspaces (`insights-onprem`/`koku`, `redhat-developer-rhdh-plugins`,
blank "no folder open" sessions). By the time the fix actually shipped (~21h later), continued
unfiltered ingestion had grown that to **221 documents actually purged** — confirming the live counts
mattered more than the plan-time estimate, and that this kept getting worse the longer it went
unfixed.

**Fix**: new shared module `project_scope.py` (same pattern as `correction_gate.py`/
`contradiction_resolution.py`) with `ALLOWED_WORKSPACE_PREFIXES` (kubernaut, dcm, engram),
`is_allowed_workspace()`, and `transcript_glob_patterns()`. Wired into `nightly-learn.py`'s
`run_hourly()`/`run_nightly()` via `workspace_prefixes=project_scope.ALLOWED_WORKSPACE_PREFIXES`, and
into `cocoindex-flows.py`'s `transcript_app` via `PatternFilePathMatcher(included_patterns=project_scope.transcript_glob_patterns())`.
Verified CocoIndex's `globset`-based matcher semantics directly before relying on them: `prefix*`
only matches within one path segment (doesn't cross `/`), so `"kubernaut*/agent-transcripts/**/*.jsonl"`
correctly matches `kubernaut` and sibling repos like `kubernaut-operator` while rejecting
`insights-onprem-koku` and blank `empty-window` sessions — confirmed with a standalone script against
real path strings before wiring it in, not just inferred from the docs.

**Purge**: new `purge-out-of-scope-memories.py` (dry-run default, `--execute` to delete) builds a
`transcript_id → project_dir_name` map by walking `~/.cursor/projects/*/agent-transcripts/**/*.jsonl`
(910 files indexed), classifies every `cursor-memory` document by its `document_metadata.transcript_id`,
and deletes (via `contradiction_resolution.delete_document()`) any resolving to a non-allowlisted
project. Documents with no `transcript_id` (355 — curated/pre-pipeline facts) or an unresolvable one
are left untouched by design. Ran dry-run, reviewed the list, got explicit approval, then executed —
but the count *grew* between dry-run (184) and execute (217) because the unfixed `cocoindex-flows.py`
service was still ingesting live. Final breakdown of the 217+4=**221 documents deleted**: 100
`empty-window`, 61 `insights-onprem-koku` (across two passes), 45 `insights-onprem` workspace, 6
plain `insights-onprem`, 6 `redhat-developer-rhdh-plugins`, 2 bare-numeric ("no folder open") sessions,
1 `insights-onprem-ros-helm-chart`.

**Deployment gotcha found while purging**: `io.vectorize.cocoindex.service` is a long-running
`--mode live` daemon (`ps` showed it running since before this session started) — editing
`cocoindex-flows.py` on disk does nothing until the process actually reloads it, since Python doesn't
hot-reload. Had to `launchctl kickstart -k gui/$(id -u)/io.vectorize.cocoindex.service` to pick up the
scoping fix, which is why the purge count kept growing until the restart. Also found `~/.hindsight/`
was missing a `project_scope.py` symlink entirely (both `nightly-learn.py` and `cocoindex-flows.py`
now `import project_scope` unconditionally) — would have crashed both scripts with
`ModuleNotFoundError` on the very next run had it not been caught immediately via the restart. Added
the missing symlink, confirmed clean restart in `~/.hindsight/logs/cocoindex-stderr.log`, then
re-ran the dry-run 15s later and watched new out-of-scope documents drop to near-zero (4 in-flight
stragglers from before the restart, since deleted) — the concrete signal the fix actually took effect
in the running process, not just on disk. `nightly-learn.py`'s hourly/nightly launchd jobs needed no
restart since they spawn a fresh process per invocation.

**Problem 2 — no regression test suite.** Added `pytest` (`requirements-dev.txt`) plus a `tests/`
directory: 101 tests across 7 files (`test_correction_gate.py`, `test_hindsight_client.py`,
`test_contradiction_resolution.py`, `test_review_contradictions.py`, `test_project_scope.py`,
`test_nightly_learn.py`, `test_cocoindex_flows.py`), all passing, running in well under a second with
zero live network/LLM/Hindsight calls — every `litellm`/`urlopen`/CocoIndex call is mocked via
`monkeypatch`. Root `conftest.py` adds the repo root and `spike/` to `sys.path` and provides
session-scoped fixtures (`nightly_learn`, `cocoindex_flows`, `review_contradictions`, `purge_script`)
for loading the hyphenated production scripts via `importlib.util.spec_from_file_location` (same
pattern already used inside `review-contradictions.py`). Explicit regression tests guard all three
bugs from the entry above: `test_hindsight_client.py::test_regression_ignores_chunks_key_even_when_present`,
`test_review_contradictions.py::TestApprove::test_regression_approve_deletes_conflicting_memory_and_retains_new_statement`,
and `test_nightly_learn.py`/`test_cocoindex_flows.py`'s `test_regression_correction_window_queued_action_*`
(asserting the retain/post call happens exactly 0 times for `action="queued"`, 1 time for
`"retain"`/`"auto_resolved"`). Also added `test_project_scope.py::TestIsAllowedWorkspace::test_substring_match_is_not_enough_must_be_prefix`
as a forward-looking regression guard against a naive `in` check ever replacing the current
`startswith` — the exact class of bug that would silently re-widen this fix's scope.

**Test-infra fragility found while writing the suite (not a production bug)**: `cocoindex-flows.py`
registers a CocoIndex `ContextKey("pg_pool")` at module-exec time, and CocoIndex raises if the same
key name is registered twice in one process. `review-contradictions.py`'s own internal
`importlib`-based load of `cocoindex-flows.py` silently assumes it's the *only* thing in the process
that ever loads that file — true in real usage (one script, one process) but false once a test suite
also loads `cocoindex-flows.py` independently (for `test_cocoindex_flows.py`) in the same pytest
session. The second load's `exec_module()` throws partway through (before reaching `hindsight_retain`'s
definition), which `review-contradictions.py`'s existing `try/except` catches and silently sets
`_HAS_RETAIN = False` — exactly the kind of silent degradation this whole test suite exists to catch,
just self-inflicted by test ordering rather than a real code path. Fixed at the fixture level, not in
production code: `conftest.py`'s `review_contradictions` fixture depends on the `cocoindex_flows`
fixture and replaces the broken `_cf`/`_HAS_RETAIN` with the one already-loaded, working module
instance. Confirmed the full 101-test suite passes regardless of file run order (ran forwards,
reversed, and various subsets).

**Rollback instructions**: revert `project_scope.py`'s wiring in `nightly-learn.py`/
`cocoindex-flows.py` (drop the `workspace_prefixes=`/`included_patterns=` arguments) to go back to
unscoped ingestion — no other rollback needed, since the purge only deleted documents, it didn't
change any retain-time behavior beyond the filter itself. The test suite has no production-code
coupling beyond what it's testing; `rm -rf tests/ conftest.py requirements-dev.txt` fully removes it.

## 2026-07-12: `gopls` MCP "Down Every Window" Was a Client Architecture Change, Not a Regression We Caused

**Context**: After a laptop reboot, the `gopls` MCP server started failing across every open Cursor
window simultaneously, even after the earlier `PATH`-resolution fix (bare `"gopls"` → absolute
`/Users/jgil/go/bin/gopls` in `~/.cursor/mcp.json`). User asked why it had been working so far if
nothing on our side changed.

**Root cause, found by diffing Cursor's own log history across sessions**:
- gopls has *never* been stable. In the prior session (2026-07-05 → 2026-07-09), the `kubernaut-v1.5`
  workspace's gopls connection alone respawned **135 times in 4 days** (~once/hour). Zero panics among
  those restarts — just routine, silent respawns.
- Critically, that session's architecture spawned **one gopls process per workspace/window**
  (`MCP user-gopls.workspaceId-<id>.<ts>.log`, nested per-window). A crash in one workspace's instance
  only killed that instance and respawned in ~200ms; every other window's gopls was untouched. Invisible
  by design.
- Sometime between 2026-07-09 and 2026-07-12, Cursor's client changed: the globally-configured `gopls`
  entry (from the top-level `~/.cursor/mcp.json`) is now served by **one shared process for the whole
  session**, not one per window (`mcp-server-user-gopls.log` at the top level, no per-window nesting).
- That shared process is fed the aggregate list of workspace roots from every open window. One of
  them — `kubernaut-v1.5` — is being advertised as a bare filesystem path instead of a `file://` URI:
  `panic: only file URIs are supported, got "" from "/Users/jgil/go/src/github.com/jordigilh/kubernaut-v1.5"`
  (`golang.org/x/tools/gopls@v0.22.0/internal/protocol/uri.go:89`, `DocumentURI.Path()`). Because the
  process is now shared, this one panic kills gopls for *every* window at once — the actual cause of
  "down every morning."
- Confirmed this is not a gopls version issue: upgraded to v0.23.0 (`go install
  golang.org/x/tools/gopls@latest`) and verified the identical panic-prone `Path()` implementation is
  still present in that release. The bug is in how the client formats/aggregates one root in the shared
  list, not in gopls.

**Fix applied**: Moved `gopls` out of the global `~/.cursor/mcp.json` entirely and into a project-scoped
`.cursor/mcp.json` for each actual Go workspace (`kubernaut`, `kubernaut-v1.5`, `kubernaut-v1.6`,
`kubernaut-operator` — verified each has a `go.mod`; `kubernaut-demo-console` and `kubernaut-docs` do
not and were left alone). This restores the old per-workspace isolation model: each Go workspace gets
its own gopls process again, so a URI panic triggered by one workspace can no longer cascade into every
other open window.

**Lesson**: when a previously-reliable local tool suddenly fails everywhere at once after an editor
update, check whether the *sharing model* changed before assuming a regression in our own config. Diffing
per-session log structure (nested per-window logs vs. a single top-level log) was the tell here — the
crash rate didn't change, only its blast radius did.

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
