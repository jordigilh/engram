//! HTTP transport for the MCP lifecycle slice.
//!
//! Shape is deliberately the same as the Python gateway: one endpoint per
//! project, SSE-framed JSON-RPC responses, `202` for notifications, and a
//! stable `initialize` result. Additions required by #211 are session ids,
//! `DELETE` termination, cancellation, and project-bound session isolation.

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use axum::body::Bytes;
use axum::extract::{Path, Query, Request, State};
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::{json, Map, Value};

use crate::aggregate::{aggregate_tools_list, Aggregation};
use crate::backend::{is_write_tool, prepare_arguments, tool_error, BackendAdapter};
use crate::catalog::{zvec_rg_arguments, Catalog, Route};
use crate::error::TypedError;
use crate::freshness::{
    attach_freshness, freshness_for_static, now_ms, CatalogState, FreshnessEnvelope, FreshnessState,
};
use crate::protocol::{
    initialize_result, method_not_found_error, request_cancelled_error, tools_list_result,
};
use crate::session::{request_id_key, SessionStore, MAX_SESSIONS_PER_PROJECT};

pub const SESSION_HEADER: &str = "mcp-session-id";

/// Whether a non-`initialize` request must carry a session header.
///
/// MCP-compliant clients always use the header after `initialize`. `Optional`
/// keeps the Python gateway's stateless behavior for migration and is opt-in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionPolicy {
    Required,
    Optional,
}

/// One project route. Static projects carry a fixed catalog (slice-1 tests);
/// registry-backed projects aggregate HTTP backends.
#[derive(Clone)]
pub struct Project {
    pub name: String,
    pub tools: Vec<Value>,
    pub backends: Option<Arc<BackendSet>>,
}

pub struct BackendSet {
    pub adapters: Vec<(String, Arc<dyn BackendAdapter>)>,
    pub catalog: tokio::sync::RwLock<Catalog>,
    reservations: tokio::sync::RwLock<BTreeMap<String, Route>>,
    refresh_lock: tokio::sync::Mutex<()>,
    freshness: tokio::sync::RwLock<FreshnessState>,
    generation: AtomicU64,
    published_generation: AtomicU64,
}

impl BackendSet {
    pub fn new(adapters: Vec<(String, Arc<dyn BackendAdapter>)>) -> Self {
        Self {
            adapters,
            catalog: tokio::sync::RwLock::new(Catalog::default()),
            reservations: tokio::sync::RwLock::new(BTreeMap::new()),
            refresh_lock: tokio::sync::Mutex::new(()),
            freshness: tokio::sync::RwLock::new(FreshnessState {
                state: Some(CatalogState::Refreshing),
                ..Default::default()
            }),
            generation: AtomicU64::new(0),
            published_generation: AtomicU64::new(0),
        }
    }

    /// Aggregate and publish only if no later-started refresh already won.
    /// Refreshes are serialized so a concurrent refresh cannot publish a
    /// catalog built from stale name reservations.
    pub async fn refresh(&self) -> Aggregation {
        let _refresh = self.refresh_lock.lock().await;
        let start = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        self.freshness.write().await.state = Some(CatalogState::Refreshing);
        let reservations = self.reservations.read().await.clone();
        let mut aggregation = aggregate_tools_list(&self.adapters, &reservations).await;
        let mut catalog = self.catalog.write().await;
        if self.published_generation.load(Ordering::SeqCst) < start {
            *catalog = aggregation.catalog.clone();
            *self.reservations.write().await = aggregation.reservations.clone();
            self.published_generation.store(start, Ordering::SeqCst);
            *self.freshness.write().await = FreshnessState {
                state: Some(CatalogState::Ready),
                generation: start,
                refreshed_at_ms: Some(now_ms()),
                backend_errors: aggregation.errors.clone(),
            };
        } else {
            aggregation.catalog = catalog.clone();
        }
        aggregation
    }

    pub async fn freshness(&self, project: &str, workspace: Option<&str>) -> FreshnessEnvelope {
        let state = self.freshness.read().await.clone();
        FreshnessEnvelope {
            project: project.to_string(),
            workspace: workspace.map(str::to_string),
            generation: self.published_generation.load(Ordering::SeqCst),
            state: state.state.unwrap_or(CatalogState::Ready),
            refreshed_at_ms: state.refreshed_at_ms,
            backend_errors: state.backend_errors,
        }
    }

    pub async fn needs_flushing(&self) -> bool {
        self.freshness.read().await.state == Some(CatalogState::Refreshing)
    }
}

impl Project {
    pub fn new(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            tools: Vec::new(),
            backends: None,
        }
    }

    pub fn with_tools(name: impl Into<String>, tools: Vec<Value>) -> Self {
        Self {
            name: name.into(),
            tools,
            backends: None,
        }
    }

    pub fn with_backends(
        name: impl Into<String>,
        adapters: Vec<(String, Arc<dyn BackendAdapter>)>,
    ) -> Self {
        Self {
            name: name.into(),
            tools: Vec::new(),
            backends: Some(Arc::new(BackendSet::new(adapters))),
        }
    }

    /// Best-effort catalog warm-up. A dead backend is simply absent from the
    /// catalog; prewarm never fails the process.
    pub async fn prewarm(&self) {
        let Some(backends) = &self.backends else {
            return;
        };
        let aggregation = backends.refresh().await;
        for (backend, error) in &aggregation.errors {
            eprintln!(
                "engram-mcp: project {:?} backend {backend:?} prewarm failed: {error}",
                self.name
            );
        }
    }
}

struct AppState {
    projects: BTreeMap<String, Arc<Project>>,
    sessions: Mutex<SessionStore>,
    in_flight: Arc<Mutex<HashMap<(String, String), tokio_util::sync::CancellationToken>>>,
    policy: SessionPolicy,
    origins: OriginPolicy,
}

struct InFlightGuard {
    map: Arc<Mutex<HashMap<(String, String), tokio_util::sync::CancellationToken>>>,
    key: (String, String),
}

impl Drop for InFlightGuard {
    fn drop(&mut self) {
        if let Ok(mut map) = self.map.lock() {
            map.remove(&self.key);
        }
    }
}

/// DNS-rebinding defense. A browser-originated request is accepted only when
/// its `Origin` is explicitly configured or is itself a loopback origin.
/// Requests without `Origin` are native MCP clients and stay allowed by
/// default.
#[derive(Debug, Clone)]
pub struct OriginPolicy {
    pub allow_no_origin: bool,
    pub allowed_origins: Vec<String>,
}

impl Default for OriginPolicy {
    fn default() -> Self {
        Self {
            allow_no_origin: true,
            allowed_origins: Vec::new(),
        }
    }
}

impl OriginPolicy {
    pub fn allows(&self, headers: &HeaderMap) -> bool {
        match headers.get(header::ORIGIN) {
            None => self.allow_no_origin,
            Some(value) => value
                .to_str()
                .map(|origin| {
                    self.allowed_origins
                        .iter()
                        .any(|allowed| allowed.eq_ignore_ascii_case(origin))
                        || is_loopback_origin(origin)
                })
                .unwrap_or(false),
        }
    }
}

/// Build the project-scoped MCP router with the MCP-default session policy.
pub fn router(projects: impl IntoIterator<Item = Project>) -> Router {
    router_with_policy(projects, SessionPolicy::Required)
}

/// Build the router with an explicit session policy. Unknown project names
/// return 404 rather than falling through to another route.
pub fn router_with_policy(
    projects: impl IntoIterator<Item = Project>,
    policy: SessionPolicy,
) -> Router {
    router_with_options(projects, policy, OriginPolicy::default())
}

/// Build the router with explicit session and origin policy.
pub fn router_with_options(
    projects: impl IntoIterator<Item = Project>,
    policy: SessionPolicy,
    origins: OriginPolicy,
) -> Router {
    let projects: BTreeMap<String, Arc<Project>> = projects
        .into_iter()
        .map(|project| (project.name.clone(), Arc::new(project)))
        .collect();
    let max_sessions = MAX_SESSIONS_PER_PROJECT * projects.len().max(1);
    let state = Arc::new(AppState {
        projects,
        sessions: Mutex::new(SessionStore::with_max_sessions(max_sessions)),
        in_flight: Arc::new(Mutex::new(HashMap::new())),
        policy,
        origins,
    });

    Router::new()
        .route(
            "/mcp/{project}",
            get(get_endpoint)
                .post(post_endpoint)
                .delete(delete_endpoint),
        )
        .layer(middleware::from_fn_with_state(state.clone(), origin_guard))
        .with_state(state)
}

async fn origin_guard(
    State(state): State<Arc<AppState>>,
    request: Request,
    next: Next,
) -> Response {
    if state.origins.allows(request.headers()) && host_is_loopback(request.headers(), request.uri())
    {
        next.run(request).await
    } else {
        StatusCode::FORBIDDEN.into_response()
    }
}

/// A missing authority is accepted for minimal HTTP clients; a present
/// authority must be loopback. HTTP/2 carries it in the URI, HTTP/1.1 in the
/// `Host` header.
fn host_is_loopback(headers: &HeaderMap, uri: &axum::http::Uri) -> bool {
    match headers.get(header::HOST) {
        Some(value) => value.to_str().map(is_loopback_authority).unwrap_or(false),
        None => uri
            .authority()
            .map(|authority| is_loopback_authority(authority.as_str()))
            .unwrap_or(true),
    }
}

/// Loopback origins are safe by construction for a loopback-bound server. A
/// rebinding attack carries the attacker's external origin and is rejected.
fn is_loopback_origin(origin: &str) -> bool {
    let lower = origin.to_ascii_lowercase();
    let Some(rest) = lower
        .strip_prefix("http://")
        .or_else(|| lower.strip_prefix("https://"))
    else {
        return false;
    };
    let authority = rest.split('/').next().unwrap_or("");
    is_loopback_authority(authority)
}

fn is_loopback_authority(authority: &str) -> bool {
    let lower = authority.to_ascii_lowercase();
    let authority = lower.rsplit('@').next().unwrap_or(&lower);
    let host = if let Some(after_bracket) = authority.strip_prefix('[') {
        after_bracket.split(']').next().unwrap_or("")
    } else {
        authority.split(':').next().unwrap_or("")
    };
    matches!(
        host.trim_end_matches('.'),
        "localhost" | "127.0.0.1" | "::1"
    )
}

async fn get_endpoint(State(state): State<Arc<AppState>>, Path(project): Path<String>) -> Response {
    if !state.projects.contains_key(&project) {
        return StatusCode::NOT_FOUND.into_response();
    }
    (
        StatusCode::METHOD_NOT_ALLOWED,
        [(header::ALLOW, "POST, DELETE")],
    )
        .into_response()
}

async fn delete_endpoint(
    State(state): State<Arc<AppState>>,
    Path(project): Path<String>,
    headers: HeaderMap,
) -> Response {
    if !state.projects.contains_key(&project) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let session_id = match session_header(&headers) {
        Ok(Some(session_id)) => session_id,
        Ok(None) | Err(()) => return StatusCode::BAD_REQUEST.into_response(),
    };

    let mut sessions = lock_sessions(&state);
    if sessions.validate(&session_id, &project) && sessions.remove(&session_id) {
        StatusCode::OK.into_response()
    } else {
        StatusCode::NOT_FOUND.into_response()
    }
}

async fn post_endpoint(
    State(state): State<Arc<AppState>>,
    Path(project): Path<String>,
    headers: HeaderMap,
    Query(query): Query<HashMap<String, String>>,
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
    // Python parity: an explicit `"id": null` is a notification.
    let message_id = message.get("id").filter(|value| !value.is_null()).cloned();
    let session_id = match session_header(&headers) {
        Ok(session_id) => session_id,
        Err(()) => return StatusCode::BAD_REQUEST.into_response(),
    };

    // A session presented on a route must belong to that route, including on
    // initialize. Unknown, deleted, or cross-project sessions are 404.
    if let Some(session_id) = &session_id {
        if !lock_sessions(&state).validate(session_id, &project) {
            return StatusCode::NOT_FOUND.into_response();
        }
    }

    if method == "initialize" {
        let Some(id) = message_id else {
            return StatusCode::ACCEPTED.into_response();
        };
        // Reuse a validated session instead of orphaning it; a reconnect loop
        // with a persisted session id must not grow the store.
        let new_session = if let Some(existing) = &session_id {
            existing.clone()
        } else {
            match lock_sessions(&state).create(&project) {
                Ok(new_session) => new_session,
                Err(_) => return StatusCode::SERVICE_UNAVAILABLE.into_response(),
            }
        };
        let Ok(header_value) = HeaderValue::from_str(&new_session) else {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        };
        let mut response = sse(initialize_result(id));
        response
            .headers_mut()
            .insert(HeaderName::from_static(SESSION_HEADER), header_value);
        return response;
    }

    if session_id.is_none() && state.policy == SessionPolicy::Required {
        return StatusCode::BAD_REQUEST.into_response();
    }

    if method == "notifications/cancelled" {
        // MCP notifications must carry the protocol version and must not carry
        // a request id. Reject malformed envelopes before mutating state.
        if message.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
            || message.get("id").is_some()
        {
            return StatusCode::BAD_REQUEST.into_response();
        }
        if let (Some(session_id), Some(request_id)) = (
            session_id.as_deref(),
            message
                .get("params")
                .and_then(|params| params.get("requestId")),
        ) {
            let key = request_id_key(request_id);
            let token = state
                .in_flight
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .get(&(session_id.to_string(), key))
                .cloned();
            if let Some(token) = token {
                // An in-flight call consumes its own cancellation entry, so a
                // later request id reuse starts clean.
                token.cancel();
                lock_sessions(&state).take_cancelled(session_id, request_id);
            } else {
                lock_sessions(&state).cancel(session_id, request_id);
            }
        }
        return StatusCode::ACCEPTED.into_response();
    }

    let Some(id) = message_id else {
        // Any other notification (including notifications/initialized) has no
        // response body by MCP contract.
        return StatusCode::ACCEPTED.into_response();
    };

    match method {
        "ping" => sse(json!({"jsonrpc": "2.0", "id": id, "result": {}})),
        "tools/list" => {
            if let Some(backends) = configured.backends.clone() {
                let aggregation = backends.refresh().await;
                for (backend, error) in &aggregation.errors {
                    eprintln!(
                        "engram-mcp: project {project:?} backend {backend:?} unavailable: {error}"
                    );
                }
                let freshness = backends.freshness(&project, None).await;
                let mut payload = tools_list_result(id, &aggregation.catalog.tools);
                attach_to_payload(&mut payload, &freshness);
                sse(payload)
            } else {
                let freshness = freshness_for_static(&project, None);
                let mut payload = tools_list_result(id, &configured.tools);
                attach_to_payload(&mut payload, &freshness);
                sse(payload)
            }
        }
        "tools/call" => {
            if configured.backends.is_none() {
                let cancelled = session_id
                    .as_deref()
                    .map(|session_id| lock_sessions(&state).take_cancelled(session_id, &id))
                    .unwrap_or(false);
                if cancelled {
                    return sse(request_cancelled_error(id));
                }
                return sse(method_not_found_error(
                    id,
                    "tools/call is not available for static projects",
                ));
            }
            let backends = configured.backends.clone().expect("checked above");
            let workspace = query.get("workspace").map(String::as_str);
            if backends.needs_flushing().await {
                let freshness = backends.freshness(&project, workspace).await;
                let mut error = TypedError::flushing()
                    .with_epoch(freshness.generation)
                    .with_retry_after_ms(1_000);
                if let Some(workspace) = workspace {
                    error = error.in_workspace(workspace);
                }
                return typed_response(id, error);
            }
            let params = message.get("params");
            let tool_name = params
                .and_then(|params| params.get("name"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let arguments = params
                .and_then(|params| params.get("arguments"))
                .cloned()
                .unwrap_or_else(|| json!({}));
            let request_key = request_id_key(&id);
            if let Some(session_id) = session_id.clone() {
                let token = tokio_util::sync::CancellationToken::new();
                let key = (session_id.clone(), request_key);
                state
                    .in_flight
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .insert(key.clone(), token.clone());
                let _guard = InFlightGuard {
                    map: Arc::clone(&state.in_flight),
                    key,
                };
                // A cancellation that arrived before token registration is
                // consumed here; one that arrives after registration cancels
                // the token and is observed by select!.
                if lock_sessions(&state).take_cancelled(&session_id, &id) {
                    return sse(request_cancelled_error(id));
                }
                let call_id = id.clone();
                tokio::select! {
                    result = handle_backend_call(
                        &backends,
                        &project,
                        call_id,
                        tool_name,
                        arguments,
                        workspace,
                    ) => result,
                    _ = token.cancelled() => cancelled_response(
                        &backends,
                        id.clone(),
                        tool_name,
                        workspace,
                    ).await,
                }
            } else {
                handle_backend_call(&backends, &project, id, tool_name, arguments, workspace).await
            }
        }
        _ => sse(method_not_found_error(id, method)),
    }
}

async fn handle_backend_call(
    backends: &BackendSet,
    project: &str,
    id: Value,
    tool_name: &str,
    arguments: Value,
    workspace: Option<&str>,
) -> Response {
    let freshness = backends.freshness(project, workspace).await;
    let route = backends.catalog.read().await.route(tool_name).cloned();
    let Some((backend_key, raw_name)) = route else {
        return tool_result(
            id,
            tool_error(format!(
                "Unknown tool: {tool_name:?} (never existed, or its backend is currently down)"
            )),
            &freshness,
        );
    };
    let Some((_, adapter)) = backends
        .adapters
        .iter()
        .find(|(key, _)| key == &backend_key)
    else {
        return tool_result(
            id,
            tool_error(format!(
                "Backend {backend_key:?} for tool {tool_name:?} is not wired up"
            )),
            &freshness,
        );
    };

    if adapter.supports_workspace() && workspace.is_none() {
        return typed_response(id, TypedError::unbound().with_epoch(freshness.generation));
    }

    let outcome = if tool_name == "search_for_pattern" && raw_name == "zvec_grep_rg" {
        let empty = Map::new();
        match zvec_rg_arguments(arguments.as_object().unwrap_or(&empty), workspace) {
            Ok(arguments) => adapter.call_tool(&raw_name, arguments).await,
            Err(error) => return tool_result(id, tool_error(error), &freshness),
        }
    } else {
        let arguments = prepare_arguments(&raw_name, arguments);
        if workspace.is_some() && adapter.supports_workspace() {
            adapter
                .call_tool_with_workspace(&raw_name, arguments, workspace.unwrap_or_default())
                .await
        } else {
            adapter.call_tool(&raw_name, arguments).await
        }
    };

    match outcome {
        Ok(result) => tool_result(id, result, &freshness),
        Err(error) if error.uncertain && is_write_tool(&raw_name) => typed_response(
            id,
            TypedError::outcome_unknown(workspace.map(str::to_string))
                .with_epoch(freshness.generation),
        ),
        Err(error) => tool_result(
            id,
            tool_error(format!("backend '{backend_key}' failed: {}", error.message)),
            &freshness,
        ),
    }
}

fn tool_result(id: Value, mut result: Value, freshness: &FreshnessEnvelope) -> Response {
    attach_freshness(&mut result, freshness);
    sse(json!({"jsonrpc": "2.0", "id": id, "result": result}))
}

fn attach_to_payload(payload: &mut Value, freshness: &FreshnessEnvelope) {
    if let Some(result) = payload.get_mut("result") {
        attach_freshness(result, freshness);
    }
}

async fn cancelled_response(
    backends: &BackendSet,
    id: Value,
    tool_name: &str,
    workspace: Option<&str>,
) -> Response {
    let route = backends.catalog.read().await.route(tool_name).cloned();
    if let Some((_, raw_name)) = route {
        if is_write_tool(&raw_name) {
            return typed_response(
                id,
                TypedError::outcome_unknown(workspace.map(str::to_string)),
            );
        }
    }
    sse(request_cancelled_error(id))
}

fn typed_response(id: Value, error: TypedError) -> Response {
    let status =
        StatusCode::from_u16(error.kind.http_status()).unwrap_or(StatusCode::SERVICE_UNAVAILABLE);
    let mut response = (status, Json(error.payload(id))).into_response();
    for (name, value) in error.headers() {
        if let (Ok(name), Ok(value)) = (
            HeaderName::from_bytes(name.as_bytes()),
            HeaderValue::from_str(&value),
        ) {
            response.headers_mut().insert(name, value);
        }
    }
    response
}

fn session_header(headers: &HeaderMap) -> Result<Option<String>, ()> {
    match headers.get(SESSION_HEADER) {
        None => Ok(None),
        Some(value) => value
            .to_str()
            .map(|value| Some(value.to_string()))
            .map_err(|_| ()),
    }
}

fn lock_sessions(state: &AppState) -> MutexGuard<'_, SessionStore> {
    state
        .sessions
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
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
    use crate::backend::BackendError;
    use async_trait::async_trait;
    use std::sync::atomic::AtomicUsize;

    #[test]
    fn tool_definition_has_required_fields() {
        let tool = tool_definition("alpha_search", "Alpha search");
        assert_eq!(tool["name"], "alpha_search");
        assert_eq!(tool["inputSchema"]["type"], "object");
    }

    struct SlowBackend {
        current: Arc<AtomicUsize>,
        max: Arc<AtomicUsize>,
    }

    #[async_trait]
    impl BackendAdapter for SlowBackend {
        async fn list_tools(&self) -> Result<Vec<Value>, BackendError> {
            let current = self.current.fetch_add(1, Ordering::SeqCst) + 1;
            self.max.fetch_max(current, Ordering::SeqCst);
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            self.current.fetch_sub(1, Ordering::SeqCst);
            Ok(vec![tool_definition("recall", "recall")])
        }

        async fn call_tool(&self, _name: &str, _arguments: Value) -> Result<Value, BackendError> {
            Ok(json!({}))
        }
    }

    #[tokio::test]
    async fn concurrent_refreshes_are_serialized() {
        let current = Arc::new(AtomicUsize::new(0));
        let max = Arc::new(AtomicUsize::new(0));
        let set = Arc::new(BackendSet::new(vec![(
            "docs".into(),
            Arc::new(SlowBackend {
                current: Arc::clone(&current),
                max: Arc::clone(&max),
            }) as Arc<dyn BackendAdapter>,
        )]));
        let first = Arc::clone(&set);
        let second = Arc::clone(&set);
        let (one, two) = tokio::join!(
            tokio::spawn(async move { first.refresh().await }),
            tokio::spawn(async move { second.refresh().await })
        );
        one.expect("first refresh");
        two.expect("second refresh");
        assert_eq!(max.load(Ordering::SeqCst), 1);
    }
}
