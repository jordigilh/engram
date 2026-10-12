from __future__ import annotations


def validate_metric_name(name: str) -> bool:
    return bool(name) and name.replace("_", "").isalnum()
