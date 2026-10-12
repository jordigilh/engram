from __future__ import annotations

from discovery.state import WorkflowState


class ValidationError(Exception):
    pass


class Validator:
    def __init__(self, allowed_ids: set[str]) -> None:
        self.allowed_ids = allowed_ids
        self.workflow_state: WorkflowState | None = None

    def set_workflow_state(self, state: WorkflowState) -> None:
        self.workflow_state = state

    def is_allowed(self, workflow_id: str) -> bool:
        if workflow_id not in self.allowed_ids:
            return False
        return self.workflow_state is None or self.workflow_state.contains(workflow_id)

    def validate(self, workflow_id: str) -> None:
        if not self.is_allowed(workflow_id):
            raise ValidationError("workflow was not in the current discovery result")
