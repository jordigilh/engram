use std::collections::HashMap;

pub struct Signal {
    pub labels: HashMap<String, String>,
}

pub struct DiscoveryFilters {
    pub component: String,
    pub environment: String,
    pub severity: String,
}

pub fn filters_from_signal(signal: &Signal) -> DiscoveryFilters {
    DiscoveryFilters {
        component: signal.labels.get("component").cloned().unwrap_or_default(),
        environment: signal
            .labels
            .get("environment")
            .cloned()
            .unwrap_or_default(),
        severity: signal.labels.get("severity").cloned().unwrap_or_default(),
    }
}

pub fn apply_label_filters(
    workflows: Vec<super::catalog::Workflow>,
    filters: &DiscoveryFilters,
) -> Vec<super::catalog::Workflow> {
    workflows
        .into_iter()
        .filter(|workflow| {
            [
                ("component", filters.component.as_str()),
                ("environment", filters.environment.as_str()),
                ("severity", filters.severity.as_str()),
            ]
            .into_iter()
            .filter(|(_, value)| !value.is_empty())
            .all(|(key, value)| {
                workflow
                    .labels
                    .get(key)
                    .is_some_and(|actual| actual == value)
            })
        })
        .collect()
}
