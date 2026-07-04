#!/usr/bin/env python3
"""One-off/rerunnable backfill for the effectiveness/proactive_recall metrics
in historical daily report JSON files and effectiveness-report.jsonl.

Why this exists: nightly-learn.py's `analyze_mcp_effectiveness` always scopes
its 24h transcript/mcp-calls window relative to `datetime.now()`. That's
correct for live nightly runs, but it means a bug fix to the *scoring logic*
(e.g. excluding subagent transcripts, or adding workspace_prefixes scoping)
never retroactively applies to already-written daily JSON snapshots — they're
frozen with whatever logic ran that night.

Rather than accept "wait a week for the rolling window to fill back up with
corrected data," this script recomputes each historical night's effectiveness
block using the *current* (fixed) scoring code, replayed against the same raw
inputs that would have been visible that night:
  - transcripts, filtered by mtime to [original_run_time - 24h, original_run_time]
  - mcp-calls.jsonl, filtered by `ts` to the same window

The original run time is reconstructed from each project's launchd
StartCalendarInterval schedule (hindsight.nightly.plist runs kubernaut at
2:01am, hindsight.nightly-dcm.plist runs dcm at 2:31am) combined with the
file's own "date" field — NOT the file's mtime. mtime is destroyed the first
time this script writes the file, which silently corrupts the window on any
second run (every backfilled file would then look like it ran "now"). The
schedule-based timestamp is stable and makes this script safely idempotent.

Usage:
    python3 backfill-effectiveness.py [--dry-run] [--since YYYY-MM-DD]

Only the outer "effectiveness" key of each daily JSON is replaced (it holds
the entire nested report dict from analyze_mcp_effectiveness — mcp_usage,
effectiveness, proactive_recall, session_distribution, etc). Everything else
in the file (corrections, reflect/triage results, bank stats, etc.) is left
untouched.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path

# nightly-learn.py has a hyphen, so it can't be `import`ed normally.
_spec = importlib.util.spec_from_file_location(
    "nightly_learn", Path(__file__).resolve().parent / "nightly-learn.py"
)
nl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nl)

# (hour, minute) each project's nightly launchd job is scheduled to start,
# per ~/Library/LaunchAgents/io.vectorize.hindsight.nightly{,-dcm}.plist.
NIGHTLY_RUN_TIME = {
    "kubernaut": (2, 1),
    "dcm": (2, 31),
}


def backfill_one(project: str, json_path: Path, dry_run: bool) -> bool:
    if not json_path.exists():
        return False
    with open(json_path) as f:
        data = json.load(f)

    # NOTE: run_nightly() stores the *entire* dict returned by
    # analyze_mcp_effectiveness() under the outer "effectiveness" key, so
    # data["effectiveness"] is itself a full report with its own nested
    # "effectiveness"/"proactive_recall"/etc sub-keys. We replace that whole
    # nested object wholesale rather than trying to merge individual keys.
    old_report = data.get("effectiveness") or {}
    report_date_str = old_report.get("date") or data.get("date")
    if not report_date_str:
        print(f"  skip {json_path.name}: no 'date' field")
        return False
    report_date = date.fromisoformat(report_date_str)

    hour, minute = NIGHTLY_RUN_TIME[project]
    end_time = datetime(report_date.year, report_date.month, report_date.day, hour, minute)

    pconfig = nl.PROJECT_CONFIGS[project]
    workspace_prefixes = pconfig.get("workspace_prefixes")

    transcripts = nl.find_recent_transcripts(
        hours=24, workspace_prefixes=workspace_prefixes, end_time=end_time
    )
    new_report = nl.analyze_mcp_effectiveness(
        transcripts,
        workspace_prefixes=workspace_prefixes,
        end_time=end_time,
        report_date=report_date,
    )

    old_sessions = old_report.get("proactive_recall", {}).get("total_sessions", "?")
    old_pct = old_report.get("proactive_recall", {}).get("recall_adoption_pct", "?")
    new_sessions = new_report["proactive_recall"]["total_sessions"]
    new_pct = new_report["proactive_recall"]["recall_adoption_pct"]
    excluded = new_report["proactive_recall"]["subagent_sessions_excluded"]
    print(
        f"  {json_path.name}: sessions {old_sessions} -> {new_sessions} "
        f"(excluded {excluded} subagent transcripts), "
        f"adoption {old_pct}% -> {new_pct}%"
    )

    if dry_run:
        return True

    data["effectiveness"] = new_report
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
    return True


def dedupe_effectiveness_log(since: date, until: date, pre_backfill_line_count: int) -> None:
    """Drop stale pre-backfill entries for the backfilled date range.

    analyze_mcp_effectiveness() appends a fresh, correctly-dated entry to
    effectiveness-report.jsonl on every call above. Lines beyond
    `pre_backfill_line_count` are those fresh entries; anything at or before
    that mark for a date in [since, until] is a stale duplicate superseded by
    a fresh one and can be dropped, so the log converges back to one entry
    per project per date instead of accumulating both old and new versions.
    """
    log_path = nl.EFFECTIVENESS_LOG
    if not log_path.exists():
        return
    with open(log_path) as f:
        lines = f.readlines()

    old_lines, new_lines = lines[:pre_backfill_line_count], lines[pre_backfill_line_count:]
    kept_old = []
    dropped = 0
    for line in old_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
            entry_date = date.fromisoformat(entry.get("date", ""))
        except (json.JSONDecodeError, ValueError):
            kept_old.append(line)
            continue
        if since <= entry_date <= until:
            dropped += 1
            continue
        kept_old.append(line)

    with open(log_path, "w") as f:
        f.writelines(kept_old + new_lines)
    print(f"\nDropped {dropped} stale pre-backfill entries from {log_path.name} "
          f"for {since}..{until}; kept {len(kept_old)} untouched + {len(new_lines)} fresh.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    ap.add_argument("--since", default=None, help="Earliest date (YYYY-MM-DD) to backfill, inclusive")
    ap.add_argument("--until", default=None, help="Latest date (YYYY-MM-DD) to backfill, inclusive (default: yesterday)")
    args = ap.parse_args()

    since = date.fromisoformat(args.since) if args.since else date(2026, 6, 27)
    until = date.fromisoformat(args.until) if args.until else date.today() - timedelta(days=1)

    pre_backfill_line_count = 0
    if nl.EFFECTIVENESS_LOG.exists():
        with open(nl.EFFECTIVENESS_LOG) as f:
            pre_backfill_line_count = sum(1 for _ in f)

    for project in ("kubernaut", "dcm"):
        suffix = nl.PROJECT_CONFIGS[project]["log_suffix"]
        print(f"\n=== {project} ===")
        d = since
        while d <= until:
            path = nl.LOG_DIR / f"{d.isoformat()}{suffix}.json"
            backfill_one(project, path, args.dry_run)
            d += timedelta(days=1)

    if not args.dry_run:
        dedupe_effectiveness_log(since, until, pre_backfill_line_count)


if __name__ == "__main__":
    main()
