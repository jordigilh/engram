from __future__ import annotations


def validate_workflow_parameters(
    required: set[str], supplied: dict[str, str]
) -> dict[str, str]:
    missing = required - supplied.keys()
    unknown = supplied.keys() - required
    if missing or unknown:
        raise ValueError(f"invalid workflow parameters: missing={missing}, unknown={unknown}")
    return supplied
