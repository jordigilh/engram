import type { Workflow, WorkflowCatalog } from "../discovery/catalog.js";
import type { WorkflowState } from "../discovery/state.js";

export interface DiscoveryResult {
  recommended: string;
  alternatives: readonly string[];
}

export interface WorkflowResult {
  workflowId: string;
  parameters: Record<string, string>;
}

export function listWorkflows(catalog: WorkflowCatalog, state: WorkflowState): Workflow[] {
  const workflows = catalog.listWorkflows();
  recordDiscoveredWorkflows(state, workflows.map((workflow) => workflow.workflowId));
  return workflows;
}

export function recordDiscoveredWorkflows(state: WorkflowState, workflowIds: readonly string[]): void {
  state.add(workflowIds);
}

export function isWorkflowInDiscoveryResult(
  workflowId: string,
  result: DiscoveryResult,
): boolean {
  return workflowId === result.recommended || result.alternatives.includes(workflowId);
}

export function authorizeSelectionDriver(workflowId: string, result: DiscoveryResult): void {
  if (!isWorkflowInDiscoveryResult(workflowId, result)) {
    throw new Error("workflow was not returned by discovery");
  }
}

export function handleSelectWorkflow(
  workflowId: string,
  result: DiscoveryResult,
  catalog: WorkflowCatalog,
): WorkflowResult {
  authorizeSelectionDriver(workflowId, result);
  const workflow = catalog.getWorkflow(workflowId);
  if (workflow === undefined) throw new Error(`unknown workflow: ${workflowId}`);
  return applySelectedWorkflow(workflow, result);
}

export function applySelectedWorkflow(
  workflow: Workflow,
  result: DiscoveryResult,
): WorkflowResult {
  return {
    workflowId: workflow.workflowId,
    parameters: lookupDiscoveredParameters(workflow.workflowId, result),
  };
}

export function lookupDiscoveredParameters(
  workflowId: string,
  result: DiscoveryResult,
): Record<string, string> {
  return { source: workflowId === result.recommended ? "recommended" : "alternative" };
}
