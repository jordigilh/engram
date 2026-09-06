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
    raise ValueError("branch must be main or release/v<major>.<minor>")
