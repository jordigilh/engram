#!/usr/bin/env python3
"""One-time recovery: reprocess ALL transcripts to rebuild the memory bank.

Run after the 2026-06-20 triage incident where a document_id bug in
rearrange_document caused ~1700 valuable memories to be lost.

This script:
  1. Backs up then resets watermarks and retained hashes
  2. Finds ALL transcripts (not just last 24h)
  3. Re-extracts corrections and instructions from every transcript
  4. Retains them via the normal pipeline (Haiku extraction)

Usage:
  python3 recover-memories.py              # dry-run: show what would be processed
  python3 recover-memories.py --apply      # actually process and retain
  python3 recover-memories.py --max-age 30 # limit to last N days (default: all)
"""

import argparse
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timedelta
from glob import glob
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "nightly_learn", Path(__file__).parent / "nightly-learn.py"
)
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def find_all_transcripts(max_age_days: int | None = None) -> list[Path]:
    """Find all transcript files, optionally limited to last N days."""
    results = []
    cutoff = None
    if max_age_days:
        cutoff = datetime.now().timestamp() - (max_age_days * 86400)

    for path_str in glob(nightly.TRANSCRIPTS_GLOB, recursive=True):
        p = Path(path_str)
        if cutoff and p.stat().st_mtime < cutoff:
            continue
        results.append(p)
    return sorted(results, key=lambda p: p.stat().st_mtime)


def main():
    parser = argparse.ArgumentParser(description="Recovery: reprocess all transcripts")
    parser.add_argument("--apply", action="store_true", help="Actually retain (default: dry-run)")
    parser.add_argument("--max-age", type=int, default=None, help="Max age in days (default: all)")
    args = parser.parse_args()

    transcripts = find_all_transcripts(args.max_age)
    log.info("Found %d transcripts total", len(transcripts))

    if not transcripts:
        log.info("No transcripts found. Nothing to recover.")
        return 0

    # Scan for learning signals (corrections + instructions) WITHOUT watermark filtering
    watermarks: dict = {}
    candidates = nightly.filter_and_scan(transcripts, watermarks)
    log.info("Transcripts with learning signals: %d / %d", len(candidates), len(transcripts))

    total_corrections = 0
    total_instructions = 0
    for path, messages, start_index in candidates:
        corrections, instructions = nightly.extract_learning_windows(
            messages, start_index=start_index
        )
        total_corrections += len(corrections)
        total_instructions += len(instructions)

    log.info("Total correction windows: %d", total_corrections)
    log.info("Total instruction windows: %d", total_instructions)
    log.info("Total windows to retain: %d", total_corrections + total_instructions)

    if not args.apply:
        log.info("Dry-run mode. Use --apply to actually retain.")
        return 0

    # Back up existing state files before resetting
    backup_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    for state_file in [nightly.WATERMARKS_PATH, nightly.RETAINED_HASHES_PATH]:
        if state_file.exists():
            backup = state_file.with_suffix(f".pre-recovery-{backup_ts}.json")
            shutil.copy2(state_file, backup)
            log.info("Backed up %s -> %s", state_file.name, backup.name)

    # Reset state for full reprocessing
    nightly.save_watermarks({})
    nightly.save_retained_hashes(set())
    log.info("Reset watermarks and retained hashes")

    # Re-scan with empty watermarks
    watermarks = {}
    seen_hashes: set = set()
    candidates = nightly.filter_and_scan(transcripts, watermarks)

    retained_total = 0
    skipped_total = 0
    errors = []

    for i, (path, messages, start_index) in enumerate(candidates):
        transcript_id = path.stem
        corrections, instructions = nightly.extract_learning_windows(
            messages, start_index=start_index
        )
        all_windows = corrections + instructions
        if not all_windows:
            continue

        try:
            result = nightly.retain_windows_deduped(all_windows, transcript_id, seen_hashes)
            retained_total += result["items_retained"]
            skipped_total += result.get("skipped_duplicates", 0)
            if (i + 1) % 10 == 0:
                log.info(
                    "  Progress: %d/%d transcripts, %d retained so far",
                    i + 1, len(candidates), retained_total,
                )
        except Exception as e:
            log.warning("Failed for %s: %s", transcript_id, e)
            errors.append({"transcript": transcript_id, "error": str(e)})

    # Save watermarks so nightly doesn't reprocess everything again
    nightly.save_watermarks(watermarks)
    nightly.save_retained_hashes(seen_hashes)

    log.info("=== Recovery complete ===")
    log.info("  Transcripts processed: %d", len(candidates))
    log.info("  Windows retained: %d", retained_total)
    log.info("  Duplicates skipped: %d", skipped_total)
    log.info("  Errors: %d", len(errors))

    # Check resulting memory count
    try:
        from urllib.request import Request, urlopen
        resp = urlopen(
            Request(
                f"{nightly.HINDSIGHT_URL}/v1/default/banks/{nightly.BANK_ID}/memories/list?limit=1",
                headers={"Accept": "application/json"},
            ),
            timeout=10,
        )
        data = json.loads(resp.read())
        log.info("  Memory count after recovery: %d", data.get("total", 0))
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
