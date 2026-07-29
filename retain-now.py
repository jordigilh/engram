#!/usr/bin/env python3
"""On-demand, single-session retain path (checkpoint-style).

Usage:
  python3 retain-now.py --session <transcript-id-or-path> [--project kubernaut|dcm|engram]

nightly-learn.py's hourly launchd job already retains corrections within
~1-2h via a periodic 2h-window sweep across every onboarded workspace -- this
script does NOT replace that. It closes a narrower, different gap: forcing an
immediate, deterministic retain of exactly one transcript (e.g. right before
a context-window compaction), reusing nightly-learn.py's extraction,
contradiction-resolution, and watermark/hash-dedup logic unchanged. See
docs/FINDINGS.md 2026-07-29 and GitHub issue #4.

This is meant for interactive/manual invocation (or a future Cursor hook),
unlike nightly-learn.py's log-file-oriented periodic jobs -- output goes to
stdout as a short human-readable summary.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from glob import glob
from pathlib import Path
from typing import Any

# nightly-learn.py's hyphenated filename means it can't be `import`ed
# normally -- load it the same way review-contradictions.py loads
# cocoindex-flows.py (see conftest.py's retain_now fixture for how tests
# swap this out for the canonical, monkeypatchable module instance).
_spec = importlib.util.spec_from_file_location(
    "nightly_learn", Path(__file__).resolve().parent / "nightly-learn.py"
)
nightly_learn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly_learn)


def resolve_session_path(session: str) -> Path | None:
    """Resolve --session to a transcript file.

    `session` is either an existing file path, or a bare transcript id (a
    file's stem) to search for under nightly_learn.TRANSCRIPTS_GLOB. Returns
    None if neither resolves.
    """
    direct = Path(session).expanduser()
    if direct.exists():
        return direct

    for path_str in glob(nightly_learn.TRANSCRIPTS_GLOB, recursive=True):
        p = Path(path_str)
        if p.stem == session:
            return p
    return None


def run_once(
    path: Path,
    watermarks: dict,
    seen_hashes: set,
    project_override: str | None = None,
) -> dict[str, Any]:
    """Retain everything new in `path` since its last-known watermark, then
    update that watermark in place so the next hourly/nightly sweep's
    filter_and_scan() sees this content as already covered.

    Deliberately skips filter_and_scan()'s has_signal pre-check (a Haiku-call-
    avoidance optimization for periodic sweeps across many files) -- this is
    invoked for exactly one file on demand, so there's no volume to gate.
    """
    tid = path.stem
    stat = path.stat()
    wm = watermarks.get(tid, {})
    prev_count = wm.get("message_count", 0)

    messages = nightly_learn.parse_transcript(path)
    corrections, instructions = nightly_learn.extract_learning_windows(
        messages, start_index=prev_count
    )
    all_windows = corrections + instructions

    project = project_override or nightly_learn.project_for_transcript_path(path)

    retain_result = nightly_learn.retain_windows_deduped(
        all_windows, tid, seen_hashes, project=project
    )

    watermarks[tid] = {
        "size": stat.st_size,
        "message_count": len(messages),
        "last_processed": nightly_learn.datetime.now().isoformat(),
    }

    return {
        "transcript_id": tid,
        "project": project,
        "corrections_detected": len(corrections),
        "instructions_detected": len(instructions),
        **retain_result,
    }


def _print_summary(result: dict[str, Any]) -> None:
    print(
        f"Retained {result['items_retained']} item(s) from {result['transcript_id']} "
        f"(project={result['project']}): "
        f"{result['corrections_detected']} correction(s), "
        f"{result['instructions_detected']} instruction(s), "
        f"{result.get('contradictions_auto_resolved', 0)} auto-resolved, "
        f"{result.get('contradictions_queued', 0)} queued, "
        f"{result.get('skipped_duplicates', 0)} duplicate(s) skipped."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="On-demand, single-session retain (checkpoint-style, not nightly-only)."
    )
    parser.add_argument(
        "--session", required=True,
        help="Transcript id (file stem) or a direct path to a transcript .jsonl file.",
    )
    parser.add_argument(
        "--project", choices=["kubernaut", "dcm", "engram"], default=None,
        help="Override project tagging when auto-resolution from the transcript's "
             "workspace path isn't reliable (e.g. a moved/renamed transcript file).",
    )
    args = parser.parse_args(argv)

    path = resolve_session_path(args.session)
    if path is None:
        print(f"Could not resolve session {args.session!r} to a transcript file.", file=sys.stderr)
        return 1

    watermarks = nightly_learn.load_watermarks()
    seen_hashes = nightly_learn.load_retained_hashes()

    result = run_once(path, watermarks, seen_hashes, project_override=args.project)

    nightly_learn.save_watermarks(watermarks)
    nightly_learn.save_retained_hashes(seen_hashes)

    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
