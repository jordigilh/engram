package selection

import (
	"fmt"

	"example.com/synthetic-workflow-discovery/internal/discovery"
)

type ValidationError struct {
	WorkflowID string
	Reason     string
}

func (e *ValidationError) Error() string {
	return fmt.Sprintf("workflow %q rejected: %s", e.WorkflowID, e.Reason)
}

type Validator struct {
	allowed map[string]struct{}
	state   *discovery.WorkflowState
}

func NewValidator(allowedWorkflowIDs []string) *Validator {
	allowed := make(map[string]struct{}, len(allowedWorkflowIDs))
	for _, id := range allowedWorkflowIDs {
		allowed[id] = struct{}{}
	}
	return &Validator{allowed: allowed}
}

func (v *Validator) SetWorkflowState(state *discovery.WorkflowState) {
	v.state = state
}

// IsAllowed requires catalog membership and, when state is attached, current
// list_workflows membership.
func (v *Validator) IsAllowed(workflowID string) bool {
	if _, ok := v.allowed[workflowID]; !ok {
		return false
	}
	return v.state == nil || v.state.Contains(workflowID)
}

func (v *Validator) Validate(workflowID string) error {
	if v.IsAllowed(workflowID) {
		return nil
	}
	return &ValidationError{WorkflowID: workflowID, Reason: "not in current discovery result"}
}
