package tools;

import catalog.Workflow;
import catalog.WorkflowCatalog;
import discovery.WorkflowState;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Tool-facing facade for workflow discovery and selection. The discovery tool
 * returns a small recommendation set; the selection tool only accepts IDs that
 * belong to the current result.
 */
public final class Workflows {

    /** Recommendation set produced by the discovery tool. */
    public record DiscoveryResult(String recommended, List<String> alternatives) {
    }

    /** Final materialized result returned to the harness. */
    public record WorkflowResult(String workflowId, Map<String, String> parameters) {
    }

    public static List<Workflow> listWorkflows(WorkflowCatalog catalog, WorkflowState state) {
        List<Workflow> workflows = catalog.listWorkflows(Map.of());
        recordDiscoveredWorkflows(
                state, workflows.stream().map(Workflow::workflowId).toList());
        return workflows;
    }

    public static void recordDiscoveredWorkflows(
            WorkflowState state, List<String> workflowIds) {
        state.add(workflowIds);
    }

    public static boolean isWorkflowInDiscoveryResult(
            String workflowId, DiscoveryResult result) {
        return workflowId.equals(result.recommended())
                || result.alternatives().contains(workflowId);
    }

    public static void authorizeSelectionDriver(String workflowId, DiscoveryResult result) {
        if (!isWorkflowInDiscoveryResult(workflowId, result)) {
            throw new IllegalStateException("workflow was not returned by discovery");
        }
    }

    public static WorkflowResult handleSelectWorkflow(
            String workflowId, DiscoveryResult result, WorkflowCatalog catalog) {
        authorizeSelectionDriver(workflowId, result);
        Workflow workflow = catalog.getWorkflow(workflowId)
                .orElseThrow(() -> new IllegalStateException("unknown workflow: " + workflowId));
        Map<String, String> parameters = new LinkedHashMap<>();
        parameters.put(
                "source",
                workflowId.equals(result.recommended()) ? "recommended" : "alternative");
        return new WorkflowResult(workflow.workflowId(), parameters);
    }
}
