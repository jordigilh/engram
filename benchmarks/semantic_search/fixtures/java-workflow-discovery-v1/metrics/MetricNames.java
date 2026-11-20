package metrics;

/**
 * Validation for metric names emitted by the discovery and selection lane.
 *
 * <p>The audit store treats metric names as opaque keys, but dashboards group
 * by name. A name therefore has to start with an ASCII letter and may only
 * contain letters, digits, and underscores. Dots and hyphens are rejected so
 * that a name cannot accidentally be interpreted as a path or a range.</p>
 *
 * <p>This validator is intentionally independent from workflow discovery: it
 * never inspects catalog entries or discovery state. It is safe to call from
 * audit emitters before a metric value is recorded, so a malformed name is
 * rejected at the source rather than at dashboard ingestion time.</p>
 */
public final class MetricNames {

    /**
     * Returns {@code true} when {@code name} is a safe dashboard metric key.
     * A null or empty name is never valid.
     */
    public static boolean isValid(String name) {
        return name != null && name.matches("[A-Za-z][A-Za-z0-9_]*");
    }
}
