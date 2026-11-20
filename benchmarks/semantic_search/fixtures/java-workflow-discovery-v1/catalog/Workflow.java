package catalog;

import java.util.Map;

/**
 * A single remediation workflow entry as published by the static catalog.
 *
 * <p>Instances are immutable value objects. The catalog owns the authoritative
 * collection and resolves entries by {@code workflowId}; discovery and
 * selection layers only ever hold references to these records. Neither
 * {@code parameters} nor {@code labels} is mutated after construction, so a
 * resolved workflow can safely be shared across a retry attempt.</p>
 *
 * @param workflowId stable identifier used by discovery and selection
 * @param parameters required execution parameter names and their descriptions
 * @param labels free-form selector labels such as component and environment
 *
 * <p>The record deliberately carries no behavior beyond data access: filter
 * matching belongs to discovery, validation belongs to selection, and
 * parameter enforcement belongs to execution.</p>
 */
public record Workflow(
        String workflowId,
        Map<String, String> parameters,
        Map<String, String> labels) {
}
