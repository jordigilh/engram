#!/usr/bin/env python3
"""Shadow-mode trial: measure whether a cheap prefilter could safely cut
Haiku call volume for correction detection, WITHOUT gating anything for
real. See docs/FINDINGS.md 2026-07-08 entries for the full context.

For every new top-level (non-subagent) user message since the last run:
  1. Call Haiku directly (the reference/control -- same call Variant B
     used in the spike, already proven at 0.97 F1 on held-out data).
  2. Evaluate two candidate prefilters against the same message (free,
     pure Python, no added API cost): loose_regex_prefilter and
     trivial_message_exclusion_filter.
  3. Log all three verdicts to ~/.hindsight/logs/prefilter-shadow.jsonl.

Nothing is gated, retained, or changed by this script. It only observes.

Run modes:
  --backfill-days N   Also process transcripts from the last N days that
                       predate this script's watermarks (one-time bootstrap
                       so the trial has a large sample immediately instead
                       of waiting on live volume alone).
  --report            Print current trial results and exit without
                       processing anything.

Intended to run periodically (every 15-30 min) via launchd
(launchd/io.vectorize.prefilter-shadow-trial.plist), fully decoupled from
the live cocoindex-flows.py / hindsight-api services -- it only reads
transcript files and calls litellm; it does not touch cocoindex-flows.py or
retain anything.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "spike"))
from classify import classify_correction  # noqa: E402
from prefilters import loose_regex_prefilter, trivial_message_exclusion_filter  # noqa: E402

STATE_DIR = Path.home() / ".hindsight"
WATERMARKS_PATH = STATE_DIR / "logs" / "prefilter-shadow-watermarks.json"
SHADOW_LOG_PATH = STATE_DIR / "logs" / "prefilter-shadow.jsonl"
LOCK_PATH = STATE_DIR / "logs" / "prefilter-shadow-trial.lock"
TRANSCRIPTS_GLOB = os.path.expanduser("~/.cursor/projects/*/agent-transcripts/**/*.jsonl")

PROJECT_PREFIXES = {
    "kubernaut": "Users-jgil-go-src-github-com-jordigilh-kubernaut",
    "dcm": "Users-jgil-go-src-github-com-dcm-project-",
    "engram": "Users-jgil-go-src-github-com-jordigilh-engram",
}


def project_for(path: str) -> str | None:
    for name, prefix in PROJECT_PREFIXES.items():
        if prefix in path:
            return name
    return None


def load_watermarks() -> dict:
    if WATERMARKS_PATH.exists():
        try:
            return json.loads(WATERMARKS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            print("Corrupt watermarks file, starting fresh")
    return {}


def save_watermarks(wm: dict) -> None:
    WATERMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATERMARKS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(wm, indent=2))
    tmp.rename(WATERMARKS_PATH)


def parse_transcript(path: str) -> list[dict]:
    messages = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return messages


import re

# System-injected templates that appear with role="user" in the transcript
# JSONL but were never typed by a human -- discovered during the shadow
# trial's first day (see docs/FINDINGS.md 2026-07-08): ~9% of raw "user"
# messages in a 1-day sample were one of these three, and Haiku sometimes
# misreads their instructional phrasing ("perform any follow-up actions") as
# a correction/instruction-violation. The old regex approach never shared
# vocabulary with these templates, so it was accidentally immune -- this is
# a real, novel false-positive source specific to a semantic classifier
# seeing 100% of raw traffic instead of a curated/pre-filtered subset.
_BOILERPLATE_PREFIXES = (
    "Briefly inform the user about the task result and perform any follow-up actions",
    "The beginning of the above subagent result is already visible to the user.",
)
# System-injected XML-style wrapper tags (see Cursor's own documented context
# injection: system_reminder, attached_files, system_notification, plus
# tool/MCP catalog dumps and environment info blocks) -- checked as a prefix
# tag match, broader than the literal-string list above, since new wrapper
# tags could appear in sessions this 1-day sample didn't happen to include.
_BOILERPLATE_TAG_RE = re.compile(
    r"^\s*<(system_reminder|attached_files|system_notification|mcp_server_catalog|user_info)\b"
)


def is_system_boilerplate(text: str) -> bool:
    stripped = text.strip()
    if any(stripped.startswith(p) for p in _BOILERPLATE_PREFIXES):
        return True
    return bool(_BOILERPLATE_TAG_RE.match(stripped))


def extract_user_text(msg: dict) -> str:
    content = msg.get("message", {}).get("content", [])
    if isinstance(content, str):
        raw = content
    else:
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
                if match:
                    texts.append(match.group(1))
                elif not text.startswith("<external_links>"):
                    texts.append(text)
        raw = "\n".join(texts).strip()

    if is_system_boilerplate(raw):
        return ""
    return raw


_log_lock = threading.Lock()


def append_shadow_log(record: dict) -> None:
    SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock:
        with open(SHADOW_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")


def process_message(text: str, project: str, transcript_id: str) -> dict:
    cls = classify_correction(text)
    record = {
        "timestamp": datetime.now().isoformat(),
        "project": project,
        "transcript_id": transcript_id,
        "text": text[:500],
        "haiku_is_correction": cls.is_correction,
        "haiku_category": cls.category,
        "haiku_confidence": cls.confidence,
        "haiku_error": cls.error,
        "regex_prefilter_would_send": loose_regex_prefilter(text),
        "exclusion_prefilter_would_send": trivial_message_exclusion_filter(text),
    }
    append_shadow_log(record)
    return record


def run(backfill_days: int | None, workers: int = 10) -> None:
    watermarks = load_watermarks()
    cutoff_days = backfill_days if backfill_days else 2  # small buffer for normal incremental runs
    cutoff = (datetime.now() - timedelta(days=cutoff_days)).timestamp()
    paths = [
        p for p in glob.glob(TRANSCRIPTS_GLOB, recursive=True)
        if os.stat(p).st_mtime >= cutoff and "/subagents/" not in p
    ]

    total_new_messages = 0
    total_processed = 0
    total_errors = 0

    for i, path in enumerate(paths):
        proj = project_for(path)
        if proj is None:
            continue
        tid = Path(path).stem
        stat = os.stat(path)
        wm = watermarks.get(tid, {})

        # Fast-path skip for unchanged files. Safe even during a backfill run:
        # a file with matching size/message_count would yield zero new
        # messages anyway (new files -- not yet in watermarks -- always have
        # message_count=0, so this never skips first-time backfill content).
        if stat.st_size <= wm.get("size", 0):
            continue

        messages = parse_transcript(path)
        prev_count = wm.get("message_count", 0)
        new_messages = messages[prev_count:]

        texts = []
        for m in new_messages:
            if m.get("role") != "user":
                continue
            text = extract_user_text(m)
            if text:
                texts.append(text)

        if texts:
            total_new_messages += len(texts)
            # I/O-bound (network) calls -- safe to parallelize per file. A
            # crash mid-file just means that file's watermark isn't advanced
            # (retried next run; a few duplicate Haiku calls, not harmful).
            with ThreadPoolExecutor(max_workers=min(workers, len(texts))) as pool:
                futures = {pool.submit(process_message, t, proj, tid): t for t in texts}
                for future in as_completed(futures):
                    try:
                        future.result()
                        total_processed += 1
                    except Exception as e:
                        total_errors += 1
                        print(f"  error processing message in {tid}: {e}")

        watermarks[tid] = {
            "size": stat.st_size,
            "message_count": len(messages),
            "last_processed": datetime.now().isoformat(),
        }
        if backfill_days and (i + 1) % 10 == 0:
            save_watermarks(watermarks)  # periodic checkpoint during long backfills
            print(f"  ...checkpoint after {i + 1}/{len(paths)} files, "
                  f"{total_processed} messages processed so far")

    save_watermarks(watermarks)
    print(f"Scanned {len(paths)} transcript file(s). "
          f"New messages: {total_new_messages}, processed: {total_processed}, errors: {total_errors}")


def report() -> None:
    if not SHADOW_LOG_PATH.exists():
        print("No shadow trial data yet.")
        return

    records = []
    with open(SHADOW_LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    n = len(records)
    if n == 0:
        print("No shadow trial data yet.")
        return

    timestamps = [r["timestamp"] for r in records if r.get("timestamp")]
    corrections = [r for r in records if r.get("haiku_is_correction")]
    n_corrections = len(corrections)

    def prefilter_stats(key: str) -> dict:
        would_send_total = sum(1 for r in records if r.get(key))
        missed = [r for r in corrections if not r.get(key)]
        caught = n_corrections - len(missed)
        recall = caught / n_corrections if n_corrections else None
        reduction = 1 - (would_send_total / n) if n else None
        return {
            "would_send_total": would_send_total,
            "reduction_pct": reduction * 100 if reduction is not None else None,
            "recall_vs_haiku": recall,
            "caught": caught,
            "missed": missed,
        }

    print(f"Shadow trial: {n} messages observed, {n_corrections} confirmed corrections by Haiku "
          f"({n_corrections / n * 100:.1f}% of traffic)")
    if timestamps:
        print(f"Window: {min(timestamps)} .. {max(timestamps)}")
    errors = sum(1 for r in records if r.get("haiku_error"))
    if errors:
        print(f"Haiku call errors: {errors} (excluded from correction counts if classification failed)")

    print()
    for key, label in [
        ("regex_prefilter_would_send", "Loose regex prefilter"),
        ("exclusion_prefilter_would_send", "Trivial-message exclusion filter"),
    ]:
        s = prefilter_stats(key)
        recall_str = f"{s['recall_vs_haiku']*100:.1f}%" if s["recall_vs_haiku"] is not None else "n/a"
        reduction_str = f"{s['reduction_pct']:.1f}%" if s["reduction_pct"] is not None else "n/a"
        print(f"{label}:")
        print(f"  Would send to Haiku: {s['would_send_total']}/{n} messages "
              f"(Haiku-call reduction: {reduction_str})")
        print(f"  Recall vs. Haiku's own confirmed corrections: {s['caught']}/{n_corrections} = {recall_str}")
        if s["missed"]:
            print(f"  MISSED corrections (Haiku said yes, prefilter would have skipped):")
            for r in s["missed"][:10]:
                print(f"    [{r.get('haiku_category')}] {r['text'][:80]}")
        print()

    print("Decision guidance: only consider adopting a prefilter if its recall vs. Haiku")
    print("is at or very near 100% -- any missed corrections above are real corrections")
    print("that would have been silently invisible to cursor-memory under that gate.")


def _acquire_lock() -> bool:
    """Best-effort lock so an unattended, every-20-min launchd job can't run
    two overlapping instances (which could race on watermarks.json) if a
    prior run is still working through a burst of new messages. Stale locks
    (owning PID no longer running) are cleared automatically.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text().strip())
            os.kill(old_pid, 0)  # raises if not running
            return False  # another instance is genuinely still running
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass  # stale or unreadable lock -- safe to reclaim
    LOCK_PATH.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-days", type=int, default=None,
                     help="One-time bootstrap: also process transcripts from the last N days")
    ap.add_argument("--report", action="store_true", help="Print results so far and exit")
    ap.add_argument("--workers", type=int, default=10, help="Concurrent Haiku calls per file")
    args = ap.parse_args()

    if args.report:
        report()
        return 0

    if not _acquire_lock():
        print("Another instance appears to be running -- skipping this cycle.")
        return 0
    try:
        run(args.backfill_days, workers=args.workers)
    finally:
        _release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
