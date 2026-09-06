from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def validate_dossier(dossier: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not dossier.get("rr_id"):
        problems.append("missing_rr_id")
    if not dossier.get("evidence"):
        problems.append("no_evidence")
    if not dossier.get("timeline"):
        problems.append("no_timeline")
    test_name = dossier.get("test", {}).get("name", "")
    if test_name.startswith("In [It]") or test_name.startswith("Timed out"):
        problems.append("weak_test_attribution")
    summary = dossier.get("summary", {})
    if summary.get("workflow_resolution_failed") and not summary.get("manual_review_required"):
        problems.append("inconsistent_terminal_state")
    if float(dossier.get("confidence", 0.0)) < 0.9:
        problems.append("low_confidence")
    return problems


def validate_directory(directory: Path) -> dict[str, Any]:
    records = []
    for path in sorted(directory.glob("**/rr-*.json")):
        dossier = json.loads(path.read_text())
        problems = validate_dossier(dossier)
        records.append({"path": str(path), "rr_id": dossier.get("rr_id"), "problems": problems})
    return {
        "dossiers": len(records),
        "passing": sum(not record["problems"] for record in records),
        "needs_review": sum(bool(record["problems"]) for record in records),
        "records": records,
    }
