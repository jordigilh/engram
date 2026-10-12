package selection

import (
	"context"

	"example.com/synthetic-workflow-discovery/internal/discovery"
)

func SelfCorrectSelection(ctx context.Context, validator *Validator, candidate string) error {
	state, ok := discovery.WorkflowStateFromContext(ctx)
	if ok {
		validator.SetWorkflowState(state)
	}
	return validator.Validate(candidate)
}

func SelectWithRetry(ctx context.Context, validator *Validator, candidate string) error {
	if err := SelfCorrectSelection(ctx, validator, candidate); err != nil {
		return retrySelection(ctx, validator, candidate)
	}
	return nil
}

func retrySelection(ctx context.Context, validator *Validator, candidate string) error {
	return SelfCorrectSelection(ctx, validator, candidate)
}
