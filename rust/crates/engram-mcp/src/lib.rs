//! MCP lifecycle and HTTP gateway scaffold for the native Engram runtime.
//!
//! Issue #211 slice 1 owns transport/session lifecycle. Slice 2 adds HTTP
//! backend routing, catalog aggregation, and Hindsight scope enforcement.
//! The Python gateway remains the production path and is not imported here.

pub mod aggregate;
pub mod backend;
pub mod catalog;
pub mod protocol;
pub mod registry;
pub mod server;
pub mod session;

pub use aggregate::{aggregate_tools_list, Aggregation};
pub use backend::{
    build_project_adapters, BackendAdapter, BackendError, HttpRelayAdapter,
    ManualMentalModelAdapter, ScopedHindsightAdapter,
};
pub use catalog::{build_catalog, Catalog};
pub use protocol::{initialize_result, tools_list_result};
pub use registry::{BackendKind, BackendSpec, ProjectSpec, Registry};
pub use server::{
    router, router_with_options, router_with_policy, BackendSet, OriginPolicy, Project,
    SessionPolicy,
};
pub use session::SessionStore;
