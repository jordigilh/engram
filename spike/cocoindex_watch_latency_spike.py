#!/usr/bin/env python3
"""Spike: isolate CocoIndex's ``localfs.walk_dir(..., live=True)`` file-watch
latency from the multi-day transcript backlog confound.

Motivated by the 2026-07-16/17 end-to-end Tier 2 test in chat: after loading
both cocoindex services live, a synthetic ``[CORRECTION]``-shaped transcript
file went unprocessed for the full ~13 minutes observed, while pre-existing
(2-day-old) files DID get processed during that window. That result was
inconclusive -- unclear whether new-file detection is broken, or just queued
far behind a large backlog of already-known files that predate the watch.

This spike removes the backlog confound entirely: it points a throwaway
CocoIndex app at a brand-new, empty temp directory (zero pre-existing files),
using the exact same primitives as the real ``transcript_app`` in
cocoindex-flows.py (``localfs.walk_dir(recursive=True, live=True)`` +
``PatternFilePathMatcher`` + ``coco.mount_each``), but with a no-op processing
function (no LLM calls, no Hindsight retain, no Postgres) so it's cheap and
fully isolated. It then measures wall-clock detection latency for three
distinct real filesystem events:

  1. A new file created inside a brand-new subdirectory (never seen before).
  2. A new file created directly inside the already-watched root directory.
  3. A modification (append) to a file already processed once.

Per cocoindex/connectors/localfs/_source.py, the live watcher is backed by
the `watchdog` package's OS-level ``Observer`` (FSEvents on macOS), armed via
``obs.start()`` *before* the initial scan runs, plus a defensive periodic
full-rescan (default 1h) "to defend against platform-specific watcher
failures (e.g. macOS FSEvents silently stopping)" -- i.e. the doc comment
itself hints this exact failure mode is a known possibility on macOS, which
is the machine this spike (and the real service) runs on.

Run (needs the hindsight venv for the cocoindex package):
    ~/.hindsight/venv/bin/python3 spike/cocoindex_watch_latency_spike.py
"""
from __future__ import annotations

import logging
import os
import pathlib
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field

# Throwaway state db, fully isolated from the real ~/.hindsight/cocoindex.db --
# must be set before cocoindex touches its default Environment.
_STATE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="cocoindex-watch-spike-state-"))
os.environ.setdefault("COCOINDEX_DB", str(_STATE_DIR / "spike.db"))

import cocoindex as coco
from cocoindex.connectors import localfs
from cocoindex.resources.file import PatternFilePathMatcher

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watch-latency-spike")

TIMEOUT_S = 90.0
POLL_S = 0.25


@dataclass
class Events:
    lock: threading.Lock = field(default_factory=threading.Lock)
    # path -> list of (wall_clock_time, content_length) per observed processing
    seen: dict[str, list[tuple[float, int]]] = field(default_factory=dict)


EVENTS = Events()


@coco.fn
async def process_file(file: localfs.File) -> None:
    """No-op sink: just record that this path was processed, and when."""
    text = await file.read_text()
    path = str(file.file_path.path)
    with EVENTS.lock:
        EVENTS.seen.setdefault(path, []).append((time.time(), len(text or "")))
    log.warning("PROCESSED %s (len=%d)", path, len(text or ""))


@coco.fn
async def watch_main(watch_dir: pathlib.Path) -> None:
    files = localfs.walk_dir(
        watch_dir,
        recursive=True,
        live=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*.jsonl"]),
    )
    await coco.mount_each(process_file, files.items())


def _wait_for_event(path: pathlib.Path, after_ts: float, min_occurrences: int, timeout_s: float = TIMEOUT_S) -> float | None:
    """Poll EVENTS for a processing occurrence of `path` timestamped after
    `after_ts`, with at least `min_occurrences` total recorded so far.
    Returns latency in seconds, or None on timeout."""
    deadline = time.time() + timeout_s
    key = str(path)
    while time.time() < deadline:
        with EVENTS.lock:
            occurrences = EVENTS.seen.get(key, [])
            fresh = [t for t, _ in occurrences if t >= after_ts]
        if len(fresh) >= 1 and len(occurrences) >= min_occurrences:
            return fresh[0] - after_ts
        time.sleep(POLL_S)
    return None


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cocoindex-watch-spike-"))
    print(f"Watch root: {tmp}")

    app = coco.App("watch-latency-spike", watch_main, watch_dir=tmp)

    def _run() -> None:
        try:
            app.update_blocking(live=True)
        except Exception as e:  # noqa: BLE001
            log.error("watcher thread crashed: %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    print("Waiting 3s for the watcher to arm before writing any files...")
    time.sleep(3.0)

    results: dict[str, float | None] = {}

    # --- Case 1: new file in a brand-new subdirectory --------------------
    subdir = tmp / "newsub"
    subdir.mkdir()
    f1 = subdir / "case1_new_subdir.jsonl"
    t0 = time.time()
    f1.write_text('{"role": "user", "message": {"content": [{"type": "text", "text": "case1"}]}}\n')
    print(f"[t=0.0s] wrote {f1.relative_to(tmp)} (new subdirectory)")
    lat = _wait_for_event(f1, t0, min_occurrences=1)
    results["new file in brand-new subdirectory"] = lat
    print(f"  -> detected after {lat:.2f}s" if lat is not None else f"  -> TIMEOUT after {TIMEOUT_S}s")

    # --- Case 2: new file directly in the already-watched root -----------
    f2 = tmp / "case2_root_new_file.jsonl"
    t0 = time.time()
    f2.write_text('{"role": "user", "message": {"content": [{"type": "text", "text": "case2"}]}}\n')
    print(f"[t=0.0s] wrote {f2.relative_to(tmp)} (root, already-watched dir)")
    lat = _wait_for_event(f2, t0, min_occurrences=1)
    results["new file in already-watched root"] = lat
    print(f"  -> detected after {lat:.2f}s" if lat is not None else f"  -> TIMEOUT after {TIMEOUT_S}s")

    # --- Case 3: modification (append) to an already-processed file ------
    t0 = time.time()
    with f2.open("a") as fh:
        fh.write('{"role": "assistant", "message": {"content": [{"type": "text", "text": "case3 appended"}]}}\n')
    print(f"[t=0.0s] appended to {f2.relative_to(tmp)} (modification of known file)")
    lat = _wait_for_event(f2, t0, min_occurrences=2)
    results["modification of already-processed file"] = lat
    print(f"  -> detected after {lat:.2f}s" if lat is not None else f"  -> TIMEOUT after {TIMEOUT_S}s")

    print("\n=== Summary ===")
    for name, lat in results.items():
        status = f"{lat:.2f}s" if lat is not None else f"TIMEOUT (>{TIMEOUT_S:.0f}s)"
        print(f"{name:45s} {status}")

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(_STATE_DIR, ignore_errors=True)
    print("\nDone. (Watcher thread is a daemon; process exit will terminate it.)")


if __name__ == "__main__":
    main()
