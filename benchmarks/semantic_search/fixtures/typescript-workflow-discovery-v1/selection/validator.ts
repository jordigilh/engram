import type { WorkflowState } from "../discovery/state.js";

export class ValidationError extends Error {}

export class WorkflowValidator {
  private workflowState: WorkflowState | undefined;

  constructor(private readonly allowedIds: ReadonlySet<string>) {}

  setWorkflowState(state: WorkflowState): void {
    this.workflowState = state;
  }

  isAllowed(workflowId: string): boolean {
    if (!this.allowedIds.has(workflowId)) return false;
    return this.workflowState === undefined || this.workflowState.contains(workflowId);
  }

  validate(workflowId: string): void {
    if (!this.isAllowed(workflowId)) {
      throw new ValidationError("workflow was not in the current discovery result");
    }
  }
}
