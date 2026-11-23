package selection;

import discovery.WorkflowState;
import java.util.Set;

/** Raised when a candidate workflow fails the selection policy. */
class ValidationException extends RuntimeException {
}

/**
 * Selection policy: an ID must be catalog-approved and, once discovery state
 * has been attached, it must also be present in the current discovery result.
 */
public final class WorkflowValidator {
    private final Set<String> allowedIds;
    private WorkflowState workflowState;

    public WorkflowValidator(Set<String> allowedIds) {
        this.allowedIds = Set.copyOf(allowedIds);
    }

    public void setWorkflowState(WorkflowState state) {
        this.workflowState = state;
    }

    public boolean isAllowed(String workflowId) {
        if (!allowedIds.contains(workflowId)) {
            return false;
        }
        return workflowState == null || workflowState.contains(workflowId);
    }

    public void validate(String workflowId) {
        if (!isAllowed(workflowId)) {
            throw new ValidationException();
        }
    }
}
