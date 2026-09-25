from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Workflow:
    workflow_id: str
    parameters: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


class WorkflowCatalog:
    def __init__(self, workflows: list[Workflow]) -> None:
        self.workflows = {workflow.workflow_id: workflow for workflow in workflows}

    def list_workflows(self, filters: dict[str, str] | None = None) -> list[Workflow]:
        workflows = list(self.workflows.values())
        if not filters:
            return workflows
        return [workflow for workflow in workflows if all(workflow.labels.get(k) == v for k, v in filters.items())]

    def list_actions(self, filters: dict[str, str]) -> list[Workflow]:
        return self.list_workflows(filters)

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        return self.workflows.get(workflow_id)
