//! Gateway freshness envelope attached to catalog and tool responses.

use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

pub const FRESHNESS_SCHEMA_VERSION: &str = "engram-freshness.v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CatalogState {
    Refreshing,
    Ready,
}

impl CatalogState {
    pub fn as_str(self) -> &'static str {
        match self {
            CatalogState::Refreshing => "refreshing",
            CatalogState::Ready => "ready",
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct FreshnessState {
    pub state: Option<CatalogState>,
    pub generation: u64,
    pub refreshed_at_ms: Option<u64>,
    pub backend_errors: BTreeMap<String, String>,
}

#[derive(Debug, Clone)]
pub struct FreshnessEnvelope {
    pub project: String,
    pub workspace: Option<String>,
    pub generation: u64,
    pub state: CatalogState,
    pub refreshed_at_ms: Option<u64>,
    pub backend_errors: BTreeMap<String, String>,
}

impl FreshnessEnvelope {
    pub fn to_json(&self) -> Value {
        json!({
            "schema_version": FRESHNESS_SCHEMA_VERSION,
            "project": self.project,
            "workspace": self.workspace,
            "catalog_generation": self.generation,
            "catalog_state": self.state.as_str(),
            "refreshed_at_ms": self.refreshed_at_ms,
            "backend_errors": self.backend_errors,
        })
    }
}

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or(0)
}

/// Merge the envelope into `_meta.engram_freshness` without discarding any
/// backend-provided `_meta`.
pub fn attach_freshness(result: &mut Value, freshness: &FreshnessEnvelope) {
    let Some(object) = result.as_object_mut() else {
        return;
    };
    let meta = object
        .entry("_meta")
        .or_insert_with(|| Value::Object(serde_json::Map::new()));
    if let Some(meta) = meta.as_object_mut() {
        meta.insert("engram_freshness".into(), freshness.to_json());
    }
}

pub fn freshness_for_static(project: &str, workspace: Option<&str>) -> FreshnessEnvelope {
    FreshnessEnvelope {
        project: project.to_string(),
        workspace: workspace.map(str::to_string),
        generation: 0,
        state: CatalogState::Ready,
        refreshed_at_ms: None,
        backend_errors: BTreeMap::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_is_additive_and_preserves_backend_meta() {
        let mut result = json!({"_meta": {"backend": "zvec"}});
        let freshness = freshness_for_static("alpha", Some("/work"));
        attach_freshness(&mut result, &freshness);
        assert_eq!(result["_meta"]["backend"], "zvec");
        assert_eq!(
            result["_meta"]["engram_freshness"]["schema_version"],
            FRESHNESS_SCHEMA_VERSION
        );
        assert_eq!(result["_meta"]["engram_freshness"]["project"], "alpha");
        assert_eq!(
            result["_meta"]["engram_freshness"]["catalog_state"],
            "ready"
        );
    }
}
