#!/usr/bin/env python3
"""Apply a complete adjudication overlay to a semantic-search qrels draft."""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


class FinalizationError(ValueError):
    """Raised when the decision overlay does not cover the qrels exactly."""


def finalize(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    if overlay.get("status") not in {"in-progress", "complete"}:
        raise FinalizationError("overlay must be an in-progress or complete decision log")

    decisions = overlay.get("decisions")
    if not isinstance(decisions, list):
        raise FinalizationError("overlay must contain a decisions list")

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for decision in decisions:
        key = (decision.get("query_id", ""), decision.get("unit_id", ""))
        if not all(key) or key in by_key:
            raise FinalizationError(f"missing or duplicate decision: {key!r}")
        grade = decision.get("grade")
        if not isinstance(grade, int) or isinstance(grade, bool) or not 0 <= grade <= 3:
            raise FinalizationError(f"invalid grade for {key!r}: {grade!r}")
        by_key[key] = decision

    output = json.loads(json.dumps(base))
    output["judgment_status"] = "adjudicated"
    output["candidate_pool_complete"] = True
    output["adjudication"] = {
        "method": "interactive user review of the source-grounded candidate pool",
        "decision_count": len(decisions),
        "decision_log": overlay.get("base_qrels"),
    }

    seen: set[tuple[str, str]] = set()
    for query in output.get("queries", []):
        query_id = query.get("id", "")
        for judgment in query.get("judgments", []):
            key = (query_id, judgment.get("unit_id", ""))
            decision = by_key.get(key)
            if decision is None:
                raise FinalizationError(f"qrels candidate has no decision: {key!r}")
            judgment["grade"] = decision["grade"]
            if decision.get("note"):
                judgment["adjudication_note"] = decision["note"]
            seen.add(key)

    extra = set(by_key) - seen
    if extra:
        raise FinalizationError(f"overlay contains candidates absent from qrels: {sorted(extra)!r}")
    if len(seen) != len(decisions):
        raise FinalizationError("decision count does not match qrels candidates")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=pathlib.Path, required=True)
    parser.add_argument("--overlay", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    base = json.loads(args.base.read_text())
    overlay = json.loads(args.overlay.read_text())
    result = finalize(base, overlay)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
