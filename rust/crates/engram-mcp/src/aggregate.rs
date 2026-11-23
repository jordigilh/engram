//! Concurrent catalog aggregation with per-backend degradation.

use std::collections::BTreeMap;
use std::sync::Arc;

use futures::future::join_all;
use serde_json::Value;

use crate::backend::BackendAdapter;
use crate::catalog::{
    build_catalog_reserved, filter_relevant_tools, inject_search_for_pattern, Catalog, Route,
};

#[derive(Debug, Clone, Default)]
pub struct Aggregation {
    pub catalog: Catalog,
    pub reservations: BTreeMap<String, Route>,
    pub errors: BTreeMap<String, String>,
}

/// Query every backend concurrently. A dead backend is excluded from the
/// catalog; the survivors still serve, matching the Python gateway.
pub async fn aggregate_tools_list(
    adapters: &[(String, Arc<dyn BackendAdapter>)],
    reservations: &BTreeMap<String, Route>,
) -> Aggregation {
    let results = join_all(adapters.iter().map(|(key, adapter)| {
        let key = key.clone();
        let adapter = Arc::clone(adapter);
        async move { (key, adapter.list_tools().await) }
    }))
    .await;

    let mut per_backend: Vec<(String, Vec<Value>)> = Vec::new();
    let mut errors = BTreeMap::new();
    for (key, result) in results {
        match result {
            Ok(tools) => per_backend.push((key.clone(), filter_relevant_tools(&key, tools))),
            Err(error) => {
                errors.insert(key, error.message);
            }
        }
    }

    let (mut catalog, reservations) = build_catalog_reserved(per_backend, reservations);
    inject_search_for_pattern(&mut catalog);
    Aggregation {
        catalog,
        reservations,
        errors,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::BackendError;
    use async_trait::async_trait;
    use serde_json::json;

    struct Fake {
        tools: Vec<Value>,
        error: Option<String>,
    }

    #[async_trait]
    impl BackendAdapter for Fake {
        async fn list_tools(&self) -> Result<Vec<Value>, BackendError> {
            match &self.error {
                Some(error) => Err(BackendError::permanent(error.clone())),
                None => Ok(self.tools.clone()),
            }
        }

        async fn call_tool(&self, _name: &str, _arguments: Value) -> Result<Value, BackendError> {
            Ok(json!({}))
        }
    }

    #[tokio::test]
    async fn merges_survivors_and_reports_failures() {
        let adapters: Vec<(String, Arc<dyn BackendAdapter>)> = vec![
            (
                "docs".into(),
                Arc::new(Fake {
                    tools: vec![json!({"name": "recall"})],
                    error: None,
                }),
            ),
            (
                "issues".into(),
                Arc::new(Fake {
                    tools: vec![json!({"name": "recall"})],
                    error: None,
                }),
            ),
            (
                "zg".into(),
                Arc::new(Fake {
                    tools: vec![json!({"name": "zvec_grep_rg"})],
                    error: None,
                }),
            ),
            (
                "dead".into(),
                Arc::new(Fake {
                    tools: vec![],
                    error: Some("down".into()),
                }),
            ),
        ];
        let aggregation = aggregate_tools_list(&adapters, &BTreeMap::new()).await;
        assert_eq!(aggregation.errors["dead"], "down");
        assert_eq!(
            aggregation.catalog.routes["docs_recall"],
            ("docs".into(), "recall".into())
        );
        assert!(aggregation
            .catalog
            .routes
            .contains_key("search_for_pattern"));
    }

    #[tokio::test]
    async fn total_failure_is_an_empty_catalog() {
        let adapters: Vec<(String, Arc<dyn BackendAdapter>)> = vec![(
            "docs".into(),
            Arc::new(Fake {
                tools: vec![],
                error: Some("down".into()),
            }),
        )];
        let aggregation = aggregate_tools_list(&adapters, &BTreeMap::new()).await;
        assert!(aggregation.catalog.routes.is_empty());
        assert!(aggregation.catalog.tools.is_empty());
    }
}
