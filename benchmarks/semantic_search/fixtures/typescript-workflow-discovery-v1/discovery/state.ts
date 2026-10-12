export class WorkflowState {
  private readonly ids = new Set<string>();

  add(workflowIds: readonly string[]): void {
    for (const workflowId of workflowIds) {
      if (workflowId.length > 0) this.ids.add(workflowId);
    }
  }

  contains(workflowId: string): boolean {
    return this.ids.has(workflowId);
  }
}

export function stateFromContext(context: Map<string, unknown>): WorkflowState {
  const state = context.get("workflowState");
  return state instanceof WorkflowState ? state : new WorkflowState();
}
