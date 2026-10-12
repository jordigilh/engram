package discovery

import "context"

type Workflow struct {
	ID         string
	Parameters map[string]string
	Labels     map[string]string
}

type WorkflowCatalog struct {
	workflows map[string]Workflow
}

func (c *WorkflowCatalog) ListWorkflows(ctx context.Context, filters DiscoveryFilters) []Workflow {
	_ = ctx
	_ = filters
	result := make([]Workflow, 0, len(c.workflows))
	for _, workflow := range c.workflows {
		result = append(result, workflow)
	}
	return result
}

// ListActions applies signal-derived filters before exposing available actions.
func (c *WorkflowCatalog) ListActions(ctx context.Context, filters DiscoveryFilters) []Workflow {
	return ApplyLabelFilters(ctx, c.ListWorkflows(ctx, filters), filters)
}

func (c *WorkflowCatalog) GetWorkflow(ctx context.Context, workflowID string) (Workflow, bool) {
	_ = ctx
	workflow, ok := c.workflows[workflowID]
	return workflow, ok
}
