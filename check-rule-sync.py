#!/usr/bin/env python3
"""Diff deployed Cursor rules against their canonical repo copies.

Lever #6 of the 2026-07-14 "reduce input tokens" review: hindsight-memory.mdc
is deployed once globally to ~/.cursor/rules/hindsight-memory.mdc (it governs
kubernaut/dcm's recall/methodology behavior), while the version under version
control here is cursor/hindsight-memory.mdc. Edit the deployed copy directly
(or forget to re-copy after editing the repo copy) and the two silently
diverge, so what the agent actually reads at runtime stops matching what's
committed and reviewed.

Extended 2026-07-15 (Engram onboarding + kubernaut-operator/console
tag-scoped recall) to cover every canonical/deployed rule pair this repo
owns, not just the one global rule -- engram, kubernaut-operator, and
kubernaut-console each got their own hand-authored workspace-level rule
(see cursor/*-hindsight-memory.mdc), each with the same drift risk.

Confirmed in the 2026-07-14 review: the global pair's two copies differed
only by cosmetic line-wrapping, not by content -- this script exists to make
that kind of check a one-command habit instead of a manual `diff` someone has
to remember to run.

Usage:
    python3 check-rule-sync.py                    # check all pairs, exit 1 if any drift
    python3 check-rule-sync.py --fix              # copy canonical -> deployed for all drifted pairs
    python3 check-rule-sync.py --pair engram      # check just one named pair
    python3 check-rule-sync.py --pair engram --fix

Exit codes: 0 = all pairs in sync (or fixed), 1 = drift detected in at least
one pair and not fixed, 2 = at least one pair has a missing canonical or
deployed copy.
"""
from __future__ import annotations

import argparse
import difflib
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
HOME = Path.home()

# Back-compat aliases for the original single-pair (global rule) shape --
# kept so the "global" entry below and any external references stay obvious.
CANONICAL = REPO_ROOT / "cursor" / "hindsight-memory.mdc"
DEPLOYED = HOME / ".cursor" / "rules" / "hindsight-memory.mdc"

# Every canonical/deployed rule pair this repo owns. Keyed by a short name
# for --pair. "global" governs kubernaut+dcm (no per-repo rule of their own);
# engram/operator/console each have their own hand-authored workspace-level
# rule instead (see docs/FINDINGS.md for why operator/console didn't get a
# physical bank split -- they use tag-scoped recall on the shared bank).
RULE_PAIRS: dict[str, tuple[Path, Path]] = {
    "global": (CANONICAL, DEPLOYED),
    "engram": (
        REPO_ROOT / "cursor" / "engram-hindsight-memory.mdc",
        REPO_ROOT / ".cursor" / "rules" / "hindsight-memory.mdc",
    ),
    "operator": (
        REPO_ROOT / "cursor" / "operator-hindsight-memory.mdc",
        HOME / "go" / "src" / "github.com" / "jordigilh" / "kubernaut-operator" / ".cursor" / "rules" / "hindsight-memory.mdc",
    ),
    "console": (
        REPO_ROOT / "cursor" / "console-hindsight-memory.mdc",
        HOME / "go" / "src" / "github.com" / "jordigilh" / "kubernaut-console" / ".cursor" / "rules" / "hindsight-memory.mdc",
    ),
}


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
        help="Copy the canonical repo copy over the deployed copy for any pair with drift",
    )
    parser.add_argument(
        "--pair", choices=sorted(RULE_PAIRS), default=None,
        help="Check only one named pair (default: check all pairs)",
    )
    args = parser.parse_args()

    # Read RULE_PAIRS as a current module global (rather than relying on
    # check_rule_sync()'s default-argument binding) so tests can monkeypatch
    # it per-call.
    names = [args.pair] if args.pair else sorted(RULE_PAIRS)

    any_missing = False
    any_unfixed_drift = False

    for name in names:
        canonical, deployed = RULE_PAIRS[name]
        result = check_rule_sync(canonical, deployed)

        if result["missing"]:
            any_missing = True
            for path in result["missing"]:
                print(f"[{name}] Missing: {path}")
            continue

        if result["in_sync"]:
            print(f"[{name}] In sync: {deployed} matches {canonical}")
            continue

        print(f"[{name}] DRIFT DETECTED between {deployed} and {canonical}:")
        print("".join(result["diff"]))

        if args.fix:
            shutil.copy2(canonical, deployed)
            print(f"[{name}] Fixed: copied {canonical} -> {deployed}")
        else:
            any_unfixed_drift = True
            print(f"[{name}] Run with --fix to copy the canonical copy over the deployed one.")

    if any_missing:
        return 2
    if any_unfixed_drift:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
