package catalog;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;

/**
 * Read-only catalog of remediation workflows. Query callers use
 * {@link #listWorkflows(Map)} to enumerate entries and {@link #getWorkflow(String)}
 * to resolve a concrete identity before execution.
 */
public final class WorkflowCatalog {
    private final List<Workflow> workflows;

    public WorkflowCatalog(List<Workflow> workflows) {
        this.workflows = List.copyOf(workflows);
    }

    public List<Workflow> listWorkflows(Map<String, String> filters) {
        List<Workflow> matched = new ArrayList<>();
        for (Workflow workflow : workflows) {
            boolean accepted = filters.entrySet().stream().allMatch(entry ->
                    Normalize.labelValue(workflow.labels().getOrDefault(entry.getKey(), ""))
                            .equals(Normalize.labelValue(entry.getValue())));
            if (accepted) {
                matched.add(workflow);
            }
        }
        return matched;
    }

    /**
     * Near-miss wrapper: forwards an already-built filter map without any
     * signal-label conversion. It looks like the discovery entry point but
     * cannot turn labels into filters.
     */
    public List<Workflow> listActions(Map<String, String> filters) {
        return listWorkflows(filters);
    }

    public Optional<Workflow> getWorkflow(String workflowId) {
        return workflows.stream()
                .filter(workflow -> workflow.workflowId().equals(workflowId))
                .findFirst();
    }
}

/** Catalog-side label normalizer, distinct from discovery.Normalize. */
final class Normalize {
    static String labelValue(String value) {
        return value == null ? "" : value.strip().toLowerCase(Locale.ROOT);
    }
}
