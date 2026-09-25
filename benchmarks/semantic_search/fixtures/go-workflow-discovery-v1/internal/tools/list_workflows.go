package tools

import (
	"context"

	"example.com/synthetic-workflow-discovery/internal/discovery"
)

func ListWorkflows(ctx context.Context, catalog *discovery.WorkflowCatalog, state *discovery.WorkflowState, filters discovery.DiscoveryFilters) []discovery.Workflow {
	workflows := catalog.ListWorkflows(ctx, filters)
	ids := make([]string, 0, len(workflows))
	for _, workflow := range workflows {
		ids = append(ids, workflow.ID)
	}
	RecordDiscovered(state, ids)
	return workflows
}

func RecordDiscovered(state *discovery.WorkflowState, workflowIDs []string) {
	if state != nil {
		state.Add(workflowIDs...)
	}
}
