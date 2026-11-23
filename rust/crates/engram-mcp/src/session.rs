//! In-memory MCP session state.
//!
//! Sessions are bound to one project. A session presented on another project's
//! route is rejected, which is the isolation property #211 asks for. A session
//! is also the unit that records cancelled request ids so a subsequent
//! `tools/call` for the same id cannot run.
//!
//! State is deliberately bounded: sessions expire, the total count is capped,
//! cancellation is one-shot, and oversized request ids are ignored.

use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

use serde_json::Value;

pub const SESSION_TTL: Duration = Duration::from_secs(30 * 60);
pub const MAX_SESSIONS: usize = 1_024;
pub const MAX_SESSIONS_PER_PROJECT: usize = 256;
pub const MAX_CANCELLED_PER_SESSION: usize = 64;
pub const MAX_REQUEST_ID_LEN: usize = 256;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionError {
    Random,
}

#[derive(Debug)]
pub struct Session {
    pub project: String,
    pub cancelled: HashSet<String>,
    last_seen: Instant,
}

#[derive(Debug)]
pub struct SessionStore {
    sessions: HashMap<String, Session>,
    max_sessions: usize,
}

impl Default for SessionStore {
    fn default() -> Self {
        Self {
            sessions: HashMap::new(),
            max_sessions: MAX_SESSIONS,
        }
    }
}

impl SessionStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Size the global cap from the configured project count so every project
    /// can reach its per-project cap without displacing another project.
    pub fn with_max_sessions(max_sessions: usize) -> Self {
        Self {
            sessions: HashMap::new(),
            max_sessions: max_sessions.max(1),
        }
    }

    /// Create a session, sweeping expired entries first. At a cap the oldest
    /// idle session is evicted, so one project's reconnect churn cannot deny
    /// `initialize` to another project.
    pub fn create(&mut self, project: &str) -> Result<String, SessionError> {
        self.sweep();
        self.make_room(project);
        let id = random_session_id()?;
        self.sessions.insert(
            id.clone(),
            Session {
                project: project.to_string(),
                cancelled: HashSet::new(),
                last_seen: Instant::now(),
            },
        );
        Ok(id)
    }

    fn make_room(&mut self, project: &str) {
        let project_count = self
            .sessions
            .values()
            .filter(|session| session.project == project)
            .count();
        if project_count >= MAX_SESSIONS_PER_PROJECT {
            self.evict_oldest(Some(project));
        }
        if self.sessions.len() >= self.max_sessions {
            // Evict the requesting project first so a churny project cannot
            // terminate another project's live sessions.
            if !self.evict_oldest(Some(project)) {
                self.evict_oldest(None);
            }
        }
    }

    fn evict_oldest(&mut self, project: Option<&str>) -> bool {
        let candidate = self
            .sessions
            .iter()
            .filter(|(_, session)| project.is_none_or(|name| session.project == name))
            .min_by_key(|(_, session)| session.last_seen)
            .map(|(id, _)| id.clone());
        match candidate {
            Some(id) => self.sessions.remove(&id).is_some(),
            None => false,
        }
    }

    /// True when the session exists, belongs to `project`, and is unexpired.
    /// Validating also refreshes its idle deadline.
    pub fn validate(&mut self, session_id: &str, project: &str) -> bool {
        self.sweep();
        let Some(session) = self.sessions.get_mut(session_id) else {
            return false;
        };
        if session.project != project {
            return false;
        }
        session.last_seen = Instant::now();
        true
    }

    /// Remove a session. Returns `false` when it never existed.
    pub fn remove(&mut self, session_id: &str) -> bool {
        self.sessions.remove(session_id).is_some()
    }

    /// Record a cancellation for a request id within a known session.
    ///
    /// Oversized ids and a full cancellation set are rejected rather than
    /// retained; the server still acknowledges the notification.
    pub fn cancel(&mut self, session_id: &str, request_id: &Value) -> bool {
        let key = request_id_key(request_id);
        if key.len() > MAX_REQUEST_ID_LEN {
            return false;
        }
        let Some(session) = self.sessions.get_mut(session_id) else {
            return false;
        };
        if session.cancelled.len() >= MAX_CANCELLED_PER_SESSION {
            return false;
        }
        session.cancelled.insert(key);
        true
    }

    /// Consume a cancellation. The fence is one-shot so a client may reuse a
    /// request id after the cancelled call has been answered.
    pub fn take_cancelled(&mut self, session_id: &str, request_id: &Value) -> bool {
        let key = request_id_key(request_id);
        self.sessions
            .get_mut(session_id)
            .map(|session| session.cancelled.remove(&key))
            .unwrap_or(false)
    }

    pub fn len(&self) -> usize {
        self.sessions.len()
    }

    pub fn is_empty(&self) -> bool {
        self.sessions.is_empty()
    }

    fn sweep(&mut self) {
        self.sweep_at(Instant::now());
    }

    fn sweep_at(&mut self, now: Instant) {
        self.sessions
            .retain(|_, session| now.duration_since(session.last_seen) < SESSION_TTL);
    }
}

/// JSON-RPC ids may be numbers or strings; normalize to one lookup key.
pub fn request_id_key(request_id: &Value) -> String {
    match request_id {
        Value::String(value) => format!("s:{value}"),
        other => format!("j:{other}"),
    }
}

fn random_session_id() -> Result<String, SessionError> {
    let mut bytes = [0_u8; 16];
    getrandom::getrandom(&mut bytes).map_err(|_| SessionError::Random)?;
    let mut id = String::with_capacity(7 + bytes.len() * 2);
    id.push_str("engram-");
    for byte in bytes {
        id.push_str(&format!("{byte:02x}"));
    }
    Ok(id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sessions_are_bound_to_a_project() {
        let mut store = SessionStore::new();
        let id = store.create("alpha").unwrap();
        assert!(store.validate(&id, "alpha"));
        assert!(!store.validate(&id, "beta"));
        assert!(!store.validate("missing", "alpha"));
    }

    #[test]
    fn deleting_a_session_revokes_it() {
        let mut store = SessionStore::new();
        let id = store.create("alpha").unwrap();
        assert!(store.remove(&id));
        assert!(!store.remove(&id));
        assert!(!store.validate(&id, "alpha"));
    }

    #[test]
    fn cancellation_is_one_shot() {
        let mut store = SessionStore::new();
        let id = store.create("alpha").unwrap();
        assert!(store.cancel(&id, &json!(1)));
        assert!(store.take_cancelled(&id, &json!(1)));
        assert!(!store.take_cancelled(&id, &json!(1)));
    }

    #[test]
    fn cancellation_keys_distinguish_numbers_and_strings() {
        let mut store = SessionStore::new();
        let id = store.create("alpha").unwrap();
        assert!(store.cancel(&id, &json!(1)));
        assert!(!store.take_cancelled(&id, &json!("1")));
    }

    #[test]
    fn cancellation_is_session_scoped() {
        let mut store = SessionStore::new();
        let alpha = store.create("alpha").unwrap();
        let beta = store.create("beta").unwrap();
        assert!(store.cancel(&alpha, &json!(7)));
        assert!(!store.take_cancelled(&beta, &json!(7)));
    }

    #[test]
    fn ids_are_unpredictable_and_unique() {
        let mut store = SessionStore::new();
        let first = store.create("alpha").unwrap();
        let second = store.create("alpha").unwrap();
        assert_ne!(first, second);
        assert_eq!(first.len(), "engram-".len() + 32);
    }

    #[test]
    fn expired_sessions_are_swept() {
        let mut store = SessionStore::new();
        let id = store.create("alpha").unwrap();
        store.sweep_at(Instant::now() + SESSION_TTL + Duration::from_secs(1));
        assert!(store.is_empty());
        assert!(!store.validate(&id, "alpha"));
    }

    #[test]
    fn oversized_cancellation_ids_are_not_retained() {
        let mut store = SessionStore::new();
        let id = store.create("alpha").unwrap();
        let long = json!("x".repeat(MAX_REQUEST_ID_LEN + 1));
        assert!(!store.cancel(&id, &long));
    }

    #[test]
    fn per_project_cap_evicts_the_oldest_without_affecting_other_projects() {
        let mut store = SessionStore::new();
        let beta = store.create("beta").unwrap();

        let mut alpha_ids = Vec::new();
        for _ in 0..=MAX_SESSIONS_PER_PROJECT {
            alpha_ids.push(store.create("alpha").unwrap());
        }

        let alpha_count = store
            .sessions
            .values()
            .filter(|session| session.project == "alpha")
            .count();
        assert_eq!(alpha_count, MAX_SESSIONS_PER_PROJECT);
        assert!(!store.validate(&alpha_ids[0], "alpha"));
        assert!(store.validate(alpha_ids.last().unwrap(), "alpha"));
        assert!(store.validate(&beta, "beta"));
    }

    #[test]
    fn global_pressure_evicts_the_requester_before_other_projects() {
        let mut store = SessionStore::with_max_sessions(MAX_SESSIONS_PER_PROJECT * 5);
        let mut alpha_ids = Vec::new();
        for _ in 0..MAX_SESSIONS_PER_PROJECT {
            alpha_ids.push(store.create("alpha").unwrap());
        }
        for project in ["beta", "gamma", "delta", "epsilon"] {
            for _ in 0..MAX_SESSIONS_PER_PROJECT {
                store.create(project).unwrap();
            }
        }
        assert_eq!(store.len(), MAX_SESSIONS_PER_PROJECT * 5);

        // epsilon is at its per-project cap and the store is globally full.
        // The next epsilon session must evict epsilon, not alpha.
        store.create("epsilon").unwrap();

        assert!(store.validate(&alpha_ids[0], "alpha"));
        let alpha_count = store
            .sessions
            .values()
            .filter(|session| session.project == "alpha")
            .count();
        assert_eq!(alpha_count, MAX_SESSIONS_PER_PROJECT);
        assert_eq!(store.len(), MAX_SESSIONS_PER_PROJECT * 5);
    }
}
