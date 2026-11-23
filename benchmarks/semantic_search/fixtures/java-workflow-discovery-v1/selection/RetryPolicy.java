package selection;

/**
 * Self-correction policy for selection.
 *
 * <p>The first candidate is validated with the shared validator. Only a
 * rejected candidate triggers the corrected ID, which is validated by the
 * exact same validator instance. Because the validator keeps its attached
 * discovery state across both attempts, a retry cannot widen the set of
 * workflows that were originally discovered. This is what preserves partial
 * results: earlier accepted IDs remain valid while the corrected candidate is
 * checked against the same membership set.</p>
 */
public final class RetryPolicy {

    /**
     * Tries {@code workflowId}; on policy rejection, retries with
     * {@code correctedId} against the same validator state.
     */
    public static String selectWithRetry(
            WorkflowValidator validator, String workflowId, String correctedId) {
        try {
            validator.validate(workflowId);
            return workflowId;
        } catch (ValidationException rejected) {
            return selfCorrectSelection(validator, correctedId);
        }
    }

    /**
     * Validates the corrected candidate without clearing or replacing the
     * discovery state that the validator already holds.
     */
    public static String selfCorrectSelection(WorkflowValidator validator, String workflowId) {
        validator.validate(workflowId);
        return workflowId;
    }
}
