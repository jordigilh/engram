//! In-memory MCP session state.
//!
//! Sessions are bound to one project. A session presented on another project's
//! route is rejected, which is the isolation property #211 asks for. A session
//! is also the unit that records cancelled request ids so a subsequent
//! `tools/call` for the same id cannot run.

use std::collections::{HashMap, HashSet};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

#[derive(Debug, Default)]
pub struct Session {
    pub project: String,
    pub cancelled: HashSet<String>,
}

#[derive(Debug, Default)]
pub struct SessionStore {
    sessions: HashMap<String, Session>,
    counter: u64,
}

impl SessionStore {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn create(&mut self, project: &str) -> String {
        self.counter = self.counter.wrapping_add(1);
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_millis())
            .unwrap_or(0);
        let id = format!("engram-{millis:x}-{:x}", self.counter);
        self.sessions.insert(
            id.clone(),
            Session {
                project: project.to_string(),
                cancelled: HashSet::new(),
            },
        );
        id
    }

    /// Project that owns the session, or `None` when the id is unknown.
    pub fn project_for(&self, session_id: &str) -> Option<&str> {
        self.sessions
            .get(session_id)
            .map(|session| session.project.as_str())
    }

    /// Remove a session. Returns `false` when it never existed.
    pub fn remove(&mut self, session_id: &str) -> bool {
        self.sessions.remove(session_id).is_some()
    }

    /// Record a cancellation for a request id within a known session.
    pub fn cancel(&mut self, session_id: &str, request_id: &Value) -> bool {
        let Some(session) = self.sessions.get_mut(session_id) else {
            return false;
        };
        session.cancelled.insert(request_id_key(request_id));
        true
    }

    pub fn is_cancelled(&self, session_id: &str, request_id: &Value) -> bool {
        self.sessions
            .get(session_id)
            .map(|session| session.cancelled.contains(&request_id_key(request_id)))
            .unwrap_or(false)
    }

    pub fn len(&self) -> usize {
        self.sessions.len()
    }

    pub fn is_empty(&self) -> bool {
        self.sessions.is_empty()
    }
}

/// JSON-RPC ids may be numbers or strings; normalize to one lookup key.
pub fn request_id_key(request_id: &Value) -> String {
    match request_id {
        Value::String(value) => format!("s:{value}"),
        other => format!("j:{other}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sessions_are_bound_to_a_project() {
        let mut store = SessionStore::new();
        let id = store.create("alpha");
        assert_eq!(store.project_for(&id), Some("alpha"));
        assert_eq!(store.project_for("missing"), None);
    }

    #[test]
    fn deleting_a_session_revokes_it() {
        let mut store = SessionStore::new();
        let id = store.create("alpha");
        assert!(store.remove(&id));
        assert!(!store.remove(&id));
        assert_eq!(store.project_for(&id), None);
    }

    #[test]
    fn cancellation_keys_distinguish_numbers_and_strings() {
        let mut store = SessionStore::new();
        let id = store.create("alpha");
        assert!(store.cancel(&id, &json!(1)));
        assert!(store.is_cancelled(&id, &json!(1)));
        assert!(!store.is_cancelled(&id, &json!("1")));
    }

    #[test]
    fn cancellation_is_session_scoped() {
        let mut store = SessionStore::new();
        let alpha = store.create("alpha");
        let beta = store.create("beta");
        assert!(store.cancel(&alpha, &json!(7)));
        assert!(!store.is_cancelled(&beta, &json!(7)));
    }
}
