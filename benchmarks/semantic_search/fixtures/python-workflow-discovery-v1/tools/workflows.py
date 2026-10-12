from __future__ import annotations

from dataclasses import dataclass

from discovery.catalog import Workflow, WorkflowCatalog
from discovery.state import WorkflowState


@dataclass
class DiscoveryResult:
    recommended: str = ""
    alternatives: list[str] | None = None


@dataclass
class WorkflowResult:
    workflow_id: str
    parameters: dict[str, str]


def list_workflows(catalog: WorkflowCatalog, state: WorkflowState) -> list[Workflow]:
    workflows = catalog.list_workflows()
    record_discovered_workflows(state, [workflow.workflow_id for workflow in workflows])
    return workflows


def record_discovered_workflows(state: WorkflowState, workflow_ids: list[str]) -> None:
    state.add(workflow_ids)


def is_workflow_in_discovery_result(workflow_id: str, result: DiscoveryResult) -> bool:
    return workflow_id == result.recommended or workflow_id in (result.alternatives or [])


def authorize_selection_driver(workflow_id: str, result: DiscoveryResult) -> None:
    if not is_workflow_in_discovery_result(workflow_id, result):
        raise ValueError("workflow was not returned by discovery")


def handle_select_workflow(
    workflow_id: str, result: DiscoveryResult, catalog: WorkflowCatalog
) -> WorkflowResult:
    authorize_selection_driver(workflow_id, result)
    workflow = catalog.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(workflow_id)
    return apply_selected_workflow(workflow, result)


def apply_selected_workflow(workflow: Workflow, result: DiscoveryResult) -> WorkflowResult:
    return WorkflowResult(workflow.workflow_id, lookup_discovered_parameters(workflow.workflow_id, result))


def lookup_discovered_parameters(workflow_id: str, result: DiscoveryResult) -> dict[str, str]:
    source = "recommended" if workflow_id == result.recommended else "alternative"
    return {"source": source}
