//! HTTP transport for the MCP lifecycle slice.
//!
//! Shape is deliberately the same as the Python gateway: one endpoint per
//! project, SSE-framed JSON-RPC responses, `202` for notifications, and a
//! stable `initialize` result. Additions required by #211 are session ids,
//! `DELETE` termination, cancellation, and project-bound session isolation.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use serde_json::{json, Value};

use crate::protocol::{
    initialize_result, method_not_found_error, request_cancelled_error, tools_list_result,
};
use crate::session::SessionStore;

pub const SESSION_HEADER: &str = "mcp-session-id";

/// One project route. Slice 1 only carries a static tool catalog; slice 2
/// replaces it with backend aggregation.
#[derive(Debug, Clone)]
pub struct Project {
    pub name: String,
    pub tools: Vec<Value>,
}

impl Project {
    pub fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            tools: Vec::new(),
        }
    }

    pub fn with_tools(name: impl Into<String>, tools: Vec<Value>) -> Self {
        Self {
            name: name.into(),
            tools,
        }
    }
}

#[derive(Debug)]
struct AppState {
    projects: BTreeMap<String, Project>,
    sessions: Mutex<SessionStore>,
}

/// Build the project-scoped MCP router. Unknown project names return 404
/// rather than falling through to another route.
pub fn router(projects: impl IntoIterator<Item = Project>) -> Router {
    let projects = projects
        .into_iter()
        .map(|project| (project.name.clone(), project))
        .collect();
    let state = Arc::new(AppState {
        projects,
        sessions: Mutex::new(SessionStore::new()),
    });

    Router::new()
        .route(
            "/mcp/{project}",
            get(get_endpoint)
                .post(post_endpoint)
                .delete(delete_endpoint),
        )
        .with_state(state)
}

async fn get_endpoint() -> StatusCode {
    // The gateway never streams server-initiated events in this slice.
    StatusCode::METHOD_NOT_ALLOWED
}

async fn delete_endpoint(
    State(state): State<Arc<AppState>>,
    Path(project): Path<String>,
    headers: HeaderMap,
) -> Response {
    if !state.projects.contains_key(&project) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let Some(session_id) = session_header(&headers) else {
        return StatusCode::BAD_REQUEST.into_response();
    };

    let removed = {
        let mut sessions = state.sessions.lock().expect("session store poisoned");
        match sessions.project_for(&session_id) {
            Some(owner) if owner == project => sessions.remove(&session_id),
            _ => false,
        }
    };
    if removed {
        StatusCode::OK.into_response()
    } else {
        StatusCode::NOT_FOUND.into_response()
    }
}

async fn post_endpoint(
    State(state): State<Arc<AppState>>,
    Path(project): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let Some(configured) = state.projects.get(&project) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let Ok(message) = serde_json::from_slice::<Value>(&body) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let Some(method) = message.get("method").and_then(Value::as_str) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let message_id = message.get("id").cloned();
    let session_id = session_header(&headers);

    if method == "initialize" {
        let id = message_id.unwrap_or(Value::Null);
        let new_session = {
            let mut sessions = state.sessions.lock().expect("session store poisoned");
            sessions.create(&project)
        };
        let mut response = sse(initialize_result(id));
        response.headers_mut().insert(
            HeaderName::from_static(SESSION_HEADER),
            HeaderValue::from_str(&new_session).expect("session id is header-safe"),
        );
        return response;
    }

    // A session presented on a route must belong to that route. Unknown,
    // deleted, or cross-project sessions are indistinguishable to the caller.
    if let Some(session_id) = &session_id {
        let owner = {
            let sessions = state.sessions.lock().expect("session store poisoned");
            sessions.project_for(session_id).map(str::to_string)
        };
        if owner.as_deref() != Some(project.as_str()) {
            return StatusCode::NOT_FOUND.into_response();
        }
    }

    if method == "notifications/cancelled" {
        if let (Some(session_id), Some(request_id)) = (
            session_id.as_deref(),
            message
                .get("params")
                .and_then(|params| params.get("requestId")),
        ) {
            let mut sessions = state.sessions.lock().expect("session store poisoned");
            sessions.cancel(session_id, request_id);
        }
        return StatusCode::ACCEPTED.into_response();
    }

    let Some(id) = message_id else {
        // Any other notification (including notifications/initialized) has no
        // response body by MCP contract.
        return StatusCode::ACCEPTED.into_response();
    };

    match method {
        "tools/list" => sse(tools_list_result(id, &configured.tools)),
        "tools/call" => {
            let cancelled = session_id
                .as_deref()
                .map(|session_id| {
                    let sessions = state.sessions.lock().expect("session store poisoned");
                    sessions.is_cancelled(session_id, &id)
                })
                .unwrap_or(false);
            if cancelled {
                sse(request_cancelled_error(id))
            } else {
                // Backend routing is #211 slice 2. Return an explicit protocol
                // error rather than pretending the call succeeded.
                sse(method_not_found_error(
                    id,
                    "tools/call (routing lands in #211 slice 2)",
                ))
            }
        }
        // Parity with the Python gateway: an unhandled request with an id is
        // acknowledged with the initialize-shaped result instead of hanging.
        _ => sse(initialize_result(id)),
    }
}

fn session_header(headers: &HeaderMap) -> Option<String> {
    headers
        .get(SESSION_HEADER)
        .and_then(|value| value.to_str().ok())
        .map(str::to_string)
}

fn sse(value: Value) -> Response {
    let body = format!("event: message\ndata: {value}\n\n");
    ([(header::CONTENT_TYPE, "text/event-stream")], body).into_response()
}

/// Convenience for tests and future registry wiring: a tool definition with
/// the minimal JSON Schema shape MCP clients accept.
pub fn tool_definition(name: &str, description: &str) -> Value {
    json!({
        "name": name,
        "description": description,
        "inputSchema": { "type": "object", "properties": {} }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_definition_has_required_fields() {
        let tool = tool_definition("alpha_search", "Alpha search");
        assert_eq!(tool["name"], "alpha_search");
        assert_eq!(tool["inputSchema"]["type"], "object");
    }
}
