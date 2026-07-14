#!/usr/bin/env python3
"""Diff the deployed global Cursor rule against this repo's canonical copy.

Lever #6 of the 2026-07-14 "reduce input tokens" review: hindsight-memory.mdc
is deployed once globally to ~/.cursor/rules/hindsight-memory.mdc (it governs
every project's recall/methodology behavior, not just this one), while the
version under version control here is cursor/hindsight-memory.mdc. That's a
single drift point rather than a per-project one, but it's still a real one --
edit the deployed copy directly (or forget to re-copy after editing the repo
copy) and the two silently diverge, so what the agent actually reads at
runtime stops matching what's committed and reviewed.

Confirmed in the 2026-07-14 review: the two copies differed only by cosmetic
line-wrapping in the cocoindex_search paragraph, not by content -- this script
exists to make that kind of check a one-command habit instead of a manual
`diff` someone has to remember to run.

Usage:
    python3 check-rule-sync.py         # report drift, exit 1 if any
    python3 check-rule-sync.py --fix   # copy canonical -> deployed on drift

Exit codes: 0 = in sync (or fixed), 1 = drift detected and not fixed,
2 = canonical or deployed copy missing entirely.
"""
from __future__ import annotations

import argparse
import difflib
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
CANONICAL = REPO_ROOT / "cursor" / "hindsight-memory.mdc"
DEPLOYED = Path.home() / ".cursor" / "rules" / "hindsight-memory.mdc"


def check_rule_sync(canonical: Path = CANONICAL, deployed: Path = DEPLOYED) -> dict:
    """Compare canonical vs. deployed rule file contents.

    Returns a dict with "in_sync" (bool or None if either file is missing),
    "diff" (unified diff lines, empty if in sync or missing), and
    "missing" (list of which path(s) don't exist, if any).
    """
    result: dict = {"in_sync": None, "diff": [], "missing": []}

    if not canonical.exists():
        result["missing"].append(str(canonical))
    if not deployed.exists():
        result["missing"].append(str(deployed))
    if result["missing"]:
        return result

    canonical_text = canonical.read_text().splitlines(keepends=True)
    deployed_text = deployed.read_text().splitlines(keepends=True)

    if canonical_text == deployed_text:
        result["in_sync"] = True
        return result

    result["in_sync"] = False
    result["diff"] = list(
        difflib.unified_diff(
            deployed_text, canonical_text,
            fromfile=str(deployed), tofile=str(canonical),
        )
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix", action="store_true",
        help="Copy the canonical repo copy over the deployed copy on drift",
    )
    args = parser.parse_args()

    # Read CANONICAL/DEPLOYED as current module globals (rather than relying
    # on check_rule_sync()'s default-argument binding) so tests can
    # monkeypatch them per-call.
    result = check_rule_sync(CANONICAL, DEPLOYED)

    if result["missing"]:
        for path in result["missing"]:
            print(f"Missing: {path}")
        return 2

    if result["in_sync"]:
        print(f"In sync: {DEPLOYED} matches {CANONICAL}")
        return 0

    print(f"DRIFT DETECTED between {DEPLOYED} and {CANONICAL}:")
    print("".join(result["diff"]))

    if args.fix:
        shutil.copy2(CANONICAL, DEPLOYED)
        print(f"Fixed: copied {CANONICAL} -> {DEPLOYED}")
        return 0

    print("Run with --fix to copy the canonical copy over the deployed one.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
