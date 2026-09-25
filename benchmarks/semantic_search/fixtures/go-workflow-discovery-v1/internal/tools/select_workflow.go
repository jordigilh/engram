package tools

import (
	"context"
	"fmt"

	"example.com/synthetic-workflow-discovery/internal/discovery"
)

type DiscoveryResult struct {
	Recommended string
	Alternatives []string
}

func isWorkflowInDiscoveryResult(workflowID string, result DiscoveryResult) bool {
	if result.Recommended == workflowID {
		return true
	}
	for _, alternative := range result.Alternatives {
		if alternative == workflowID {
			return true
		}
	}
	return false
}

func authorizeSelectionDriver(workflowID string, result DiscoveryResult) error {
	if !isWorkflowInDiscoveryResult(workflowID, result) {
		return fmt.Errorf("workflow %q was not discovered", workflowID)
	}
	return nil
}

func HandleSelectWorkflow(ctx context.Context, workflowID string, result DiscoveryResult, catalog *discovery.WorkflowCatalog) (WorkflowResult, error) {
	if err := authorizeSelectionDriver(workflowID, result); err != nil {
		return WorkflowResult{}, err
	}
	workflow, ok := catalog.GetWorkflow(ctx, workflowID)
	if !ok {
		return WorkflowResult{}, fmt.Errorf("workflow %q not found", workflowID)
	}
	return applySelectedWorkflow(workflow, result), nil
}

type WorkflowResult struct {
	ID         string
	Parameters map[string]string
}

func applySelectedWorkflow(workflow discovery.Workflow, result DiscoveryResult) WorkflowResult {
	return WorkflowResult{ID: workflow.ID, Parameters: lookupDiscoveredParameters(workflow.ID, result)}
}

func lookupDiscoveredParameters(workflowID string, result DiscoveryResult) map[string]string {
	if workflowID == result.Recommended {
		return map[string]string{"source": "recommended"}
	}
	return map[string]string{"source": "alternative"}
}
