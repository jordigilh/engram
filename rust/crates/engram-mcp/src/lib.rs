//! MCP lifecycle scaffold for the native Engram runtime (issue #211, slice 1).
//!
//! This crate owns exactly one slice: transport and session lifecycle parity
//! for `initialize`, notifications, `tools/list`, `DELETE`, cancellation, and
//! per-project isolation. It deliberately has no backend routing; that is
//! slice 2 of #211. The Python gateway remains the production path and is not
//! imported or modified here.

pub mod protocol;
pub mod server;
pub mod session;

pub use protocol::{initialize_result, tools_list_result};
pub use server::{router, Project};
pub use session::SessionStore;
