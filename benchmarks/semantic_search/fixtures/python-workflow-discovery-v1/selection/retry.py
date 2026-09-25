from __future__ import annotations

from selection.validator import Validator


def select_with_retry(validator: Validator, workflow_id: str, corrected_id: str) -> str:
    try:
        validator.validate(workflow_id)
        return workflow_id
    except Exception:
        return self_correct_selection(validator, corrected_id)


def self_correct_selection(validator: Validator, workflow_id: str) -> str:
    validator.validate(workflow_id)
    return workflow_id
