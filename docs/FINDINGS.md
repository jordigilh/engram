# Research Findings

Historical record of empirical findings from running Engram in production.

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
