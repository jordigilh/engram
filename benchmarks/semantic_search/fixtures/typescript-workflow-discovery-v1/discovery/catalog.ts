export interface Workflow {
  workflowId: string;
  parameters: Record<string, string>;
  labels: Record<string, string>;
}

export class WorkflowCatalog {
  constructor(private readonly workflows: readonly Workflow[]) {}

  listWorkflows(filters: Record<string, string> = {}): Workflow[] {
    return this.workflows.filter((workflow) =>
      Object.entries(filters).every(([key, value]) => workflow.labels[key] === value),
    );
  }

  listActions(filters: Record<string, string>): Workflow[] {
    return this.listWorkflows(filters);
  }

  getWorkflow(workflowId: string): Workflow | undefined {
    return this.workflows.find((workflow) => workflow.workflowId === workflowId);
  }
}
