from __future__ import annotations

import re

_RELEASE = re.compile(r"^(?:release/)?(v\d+\.\d+)$")


def normalize_branch(branch: str) -> str:
    value = branch.strip()
    if value == "main":
        return value
    match = _RELEASE.fullmatch(value)
    if match:
        return match.group(1)
    # Feature/fix branches are evaluated against their target line. GitHub
    # Actions commonly reports the source branch for push-triggered runs, so
    # rejecting it would drop otherwise valid dossiers. Callers should pass
    # the PR base branch when it is available; this fallback keeps direct CI
    # runs safely scoped to main rather than creating an unbounded branch.
    return "main"
