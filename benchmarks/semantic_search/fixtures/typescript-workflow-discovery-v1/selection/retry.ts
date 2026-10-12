import type { WorkflowValidator } from "./validator.js";

export function selectWithRetry(
  validator: WorkflowValidator,
  workflowId: string,
  correctedId: string,
): string {
  try {
    validator.validate(workflowId);
    return workflowId;
  } catch {
    return selfCorrectSelection(validator, correctedId);
  }
}

export function selfCorrectSelection(validator: WorkflowValidator, workflowId: string): string {
  validator.validate(workflowId);
  return workflowId;
}
