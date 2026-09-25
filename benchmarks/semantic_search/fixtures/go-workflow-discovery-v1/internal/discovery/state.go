package discovery

import (
	"context"
	"sync"
)

// WorkflowState records workflow IDs returned by list_workflows for one
// selection context. Retries and later discovery calls share this object.
type WorkflowState struct {
	mu  sync.RWMutex
	ids map[string]struct{}
}

func NewWorkflowState() *WorkflowState {
	return &WorkflowState{ids: make(map[string]struct{})}
}

// Add records every non-empty workflow ID returned by discovery.
func (s *WorkflowState) Add(workflowIDs ...string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ids == nil {
		s.ids = make(map[string]struct{})
	}
	for _, id := range workflowIDs {
		if id != "" {
			s.ids[id] = struct{}{}
		}
	}
}

func (s *WorkflowState) Contains(workflowID string) bool {
	if s == nil {
		return false
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	_, ok := s.ids[workflowID]
	return ok
}

type workflowStateKey struct{}

func WithWorkflowState(ctx context.Context, state *WorkflowState) context.Context {
	return context.WithValue(ctx, workflowStateKey{}, state)
}

func WorkflowStateFromContext(ctx context.Context) (*WorkflowState, bool) {
	state, ok := ctx.Value(workflowStateKey{}).(*WorkflowState)
	return state, ok && state != nil
}
