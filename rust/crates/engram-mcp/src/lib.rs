//! MCP lifecycle and HTTP gateway scaffold for the native Engram runtime.
//!
//! Issue #211 slice 1 owns transport/session lifecycle. Slice 2 adds HTTP
//! backend routing, catalog aggregation, and Hindsight scope enforcement.
//! The Python gateway remains the production path and is not imported here.

pub mod aggregate;
pub mod backend;
pub mod catalog;
pub mod error;
pub mod freshness;
pub mod metrics;
pub mod protocol;
pub mod registry;
pub mod server;
pub mod session;
pub mod shaping;
pub mod stdio;

pub use aggregate::{aggregate_tools_list, Aggregation};
pub use backend::{
    build_project_adapters, build_project_adapters_with_cache, AdapterCache, BackendAdapter,
    BackendError, HttpRelayAdapter, ManualMentalModelAdapter, ScopedHindsightAdapter,
};
pub use catalog::{build_catalog, Catalog};
pub use error::{ErrorKind, TypedError};
pub use freshness::{FreshnessEnvelope, FRESHNESS_SCHEMA_VERSION};
pub use metrics::{estimate_tokens, extract_result_text, MetricsLog};
pub use protocol::{initialize_result, tools_list_result};
pub use registry::{BackendKind, BackendSpec, ProjectSpec, Registry};
pub use server::{
    router, router_with_options, router_with_policy, BackendSet, OriginPolicy, Project,
    SessionPolicy,
};
pub use session::SessionStore;
pub use shaping::{coerce_max_answer_chars, normalize_recall_result, truncate_pattern_result};
pub use stdio::StdioSubprocessAdapter;
