package execution;

import catalog.Workflow;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * Executes a previously selected workflow. Parameter validation happens before
 * any execution side effect: required names must be supplied and unknown names
 * must be rejected.
 */
public final class WorkflowRunner {
    /** Upper bound on accepted execution parameters, guarding oversized input. */
    private static final int MAX_PARAMETERS = 64;

    public static Map<String, String> validateParameters(
            Set<String> required, Map<String, String> supplied) {
        if (supplied.size() > MAX_PARAMETERS) {
            throw new IllegalArgumentException("too many workflow parameters");
        }
        Set<String> missing = new java.util.LinkedHashSet<>(required);
        missing.removeAll(supplied.keySet());
        Set<String> unknown = new java.util.LinkedHashSet<>(supplied.keySet());
        unknown.removeAll(required);
        if (!missing.isEmpty() || !unknown.isEmpty()) {
            throw new IllegalArgumentException(
                    "invalid workflow parameters: missing=" + missing + ", unknown=" + unknown);
        }
        return supplied;
    }

    public static Map<String, String> run(Workflow workflow, Map<String, String> supplied) {
        Map<String, String> accepted =
                validateParameters(workflow.parameters().keySet(), supplied);
        return new LinkedHashMap<>(accepted);
    }
}
