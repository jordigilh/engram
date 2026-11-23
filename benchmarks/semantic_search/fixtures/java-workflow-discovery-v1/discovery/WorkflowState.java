package discovery;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Shared per-investigation state holding the workflow IDs that discovery
 * actually returned. Selection consults this state so that a catalog entry
 * which was never discovered cannot be chosen.
 *
 * <p>The set is insertion-ordered and only grows during one investigation.
 * An empty set is meaningful: it represents a discovery call that returned
 * nothing and therefore makes every catalog ID fail membership validation.</p>
 */
public final class WorkflowState {
    private final Set<String> ids = new LinkedHashSet<>();

    public void add(List<String> workflowIds) {
        for (String workflowId : workflowIds) {
            if (workflowId != null && !workflowId.isBlank()) {
                ids.add(workflowId);
            }
        }
    }

    public boolean contains(String workflowId) {
        return ids.contains(workflowId);
    }

    public static WorkflowState stateFromContext(Map<String, Object> context) {
        Object state = context.get("workflowState");
        return state instanceof WorkflowState existing ? existing : new WorkflowState();
    }
}
