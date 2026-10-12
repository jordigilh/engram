from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WorkflowState:
    """Hold workflow IDs returned by discovery for later validation."""

    ids: set[str] = field(default_factory=set)

    def add(self, workflow_ids: list[str]) -> None:
        self.ids.update(workflow_id for workflow_id in workflow_ids if workflow_id)

    def contains(self, workflow_id: str) -> bool:
        return workflow_id in self.ids


def state_from_context(context: dict[str, object]) -> WorkflowState:
    state = context.get("workflow_state")
    return state if isinstance(state, WorkflowState) else WorkflowState()
