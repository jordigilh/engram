package discovery;

import catalog.Workflow;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Discovery filters derived from harness signal labels. An empty field means
 * "not constrained"; populated fields must match the workflow labels.
 */
public record DiscoveryFilters(String component, String environment, String severity) {

    public static DiscoveryFilters fromSignal(Map<String, String> labels) {
        return new DiscoveryFilters(
                Normalize.term(labels.get("component")),
                Normalize.term(labels.get("environment")),
                Normalize.term(labels.get("severity")));
    }

    public static List<Workflow> applyLabelFilters(
            List<Workflow> workflows, DiscoveryFilters filters) {
        Map<String, String> expected = new LinkedHashMap<>();
        if (!filters.component().isEmpty()) {
            expected.put("component", filters.component());
        }
        if (!filters.environment().isEmpty()) {
            expected.put("environment", filters.environment());
        }
        if (!filters.severity().isEmpty()) {
            expected.put("severity", filters.severity());
        }
        return workflows.stream()
                .filter(workflow -> expected.entrySet().stream().allMatch(entry ->
                        Normalize.term(workflow.labels().getOrDefault(entry.getKey(), ""))
                                .equals(entry.getValue())))
                .toList();
    }
}

/** Discovery-side term normalizer, distinct from catalog.Normalize. */
final class Normalize {
    static String term(String value) {
        return value == null ? "" : value.strip().toLowerCase(Locale.ROOT);
    }
}
