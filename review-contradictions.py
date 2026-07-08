#!/usr/bin/env python3
"""Interactive review of contradictions flagged by the Semantic Correction
Detection Spike's contradiction check.

Lists each entry in ~/.hindsight/logs/contradictions-pending.jsonl (new
statement vs. the existing memory it appears to conflict with, plus the
model's explanation) and lets you:

  [a]pprove  -- retain the new statement into cursor-memory (tagged as
               superseding the conflicting memory) and remove from the queue
  [r]eject   -- discard the new statement, keep the existing memory, remove
               from the queue
  [s]kip     -- leave it in the queue for next time
  [q]uit     -- stop, leaving remaining entries untouched

Usage:
    python3 review-contradictions.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "spike"))
from pending_queue import load_pending, remove_pending  # noqa: E402

# cocoindex-flows.py has a hyphen, so it can't be `import`ed normally.
_spec = importlib.util.spec_from_file_location(
    "cocoindex_flows", Path(__file__).resolve().parent / "cocoindex-flows.py"
)
_cf = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_cf)
    _HAS_RETAIN = True
except Exception as e:  # pragma: no cover - only if cocoindex deps missing
    print(f"Note: could not import cocoindex-flows.py ({e}); approve will be disabled.")
    _HAS_RETAIN = False


def _prompt(entry: dict, index: int, total: int) -> str:
    print(f"\n{'=' * 70}")
    print(f"[{index}/{total}]  {entry.get('timestamp', '?')}  project={entry.get('project', '?')}")
    print(f"{'=' * 70}")
    print(f"NEW statement:\n  {entry['new_statement']}")
    print(f"\nConflicts with existing memory (index {entry.get('conflicting_memory_index')}):")
    print(f"  {entry['conflicting_memory']}")
    print(f"\nModel's explanation:\n  {entry['explanation']}")
    print()
    while True:
        choice = input("[a]pprove / [r]eject / [s]kip / [q]uit > ").strip().lower()
        if choice in ("a", "r", "s", "q"):
            return choice
        print("Please enter a, r, s, or q.")


def main() -> int:
    entries = load_pending()
    if not entries:
        print("No pending contradictions. Nothing to review.")
        return 0

    print(f"{len(entries)} pending contradiction(s) to review.\n")
    total = len(entries)
    for i, entry in enumerate(entries, 1):
        choice = _prompt(entry, i, total)
        if choice == "q":
            print("Stopping. Remaining entries left in the queue.")
            break
        if choice == "s":
            continue
        if choice == "r":
            remove_pending(entry["id"])
            print("Rejected -- discarded, existing memory kept.")
            continue
        if choice == "a":
            if not _HAS_RETAIN:
                print("Cannot approve: hindsight_retain unavailable in this environment. Skipping.")
                continue
            result = _cf.hindsight_retain(
                bank_id="cursor-memory",
                content=entry["new_statement"],
                document_id=f"contradiction-resolved-{entry['id']}",
                metadata={
                    "source": "review-contradictions",
                    "supersedes": entry.get("conflicting_memory"),
                },
                tags=["CORRECTION", "supersedes-prior-memory"],
            )
            remove_pending(entry["id"])
            ok = bool(result)
            print(f"{'Approved and retained.' if ok else 'Retain call failed, but removed from queue -- check logs.'}")

    remaining = load_pending()
    print(f"\n{len(remaining)} entr{'y' if len(remaining) == 1 else 'ies'} left in the queue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
