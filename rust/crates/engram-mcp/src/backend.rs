//! Backend adapters for the HTTP gateway slice.
//!
//! Stdio and shadow-comparison adapters are deliberately rejected at build
//! time; the Rust gateway is HTTP-only until their parity slice lands.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::{json, Map, Value};
use thiserror::Error;
use url::Url;

use crate::catalog::{is_retryable_raw_tool, managed_zvec_arguments};
use crate::registry::{BackendKind, ProjectSpec};

#[derive(Debug, Clone, Error)]
#[error("{message}")]
pub struct BackendError {
    pub message: String,
    pub transient: bool,
}

impl BackendError {
    pub fn permanent(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            transient: false,
        }
    }

    pub fn transient(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            transient: true,
        }
    }
}

pub type AdapterList = Vec<(String, Arc<dyn BackendAdapter>)>;

#[async_trait]
pub trait BackendAdapter: Send + Sync {
    async fn list_tools(&self) -> Result<Vec<Value>, BackendError>;
    async fn call_tool(&self, name: &str, arguments: Value) -> Result<Value, BackendError>;

    async fn call_tool_with_workspace(
        &self,
        name: &str,
        arguments: Value,
        _workspace: &str,
    ) -> Result<Value, BackendError> {
        self.call_tool(name, arguments).await
    }

    fn supports_workspace(&self) -> bool {
        false
    }
}

pub const MAX_FORWARD_TIMEOUT_S: f64 = 600.0;
pub const HTTP_RETRY_ATTEMPTS: usize = 2;
pub const HTTP_RETRY_DELAY: Duration = Duration::from_millis(250);
pub const MAX_TOOL_PAGES: usize = 100;

pub struct HttpRelayAdapter {
    url: Url,
    headers: reqwest::header::HeaderMap,
    client: reqwest::Client,
    workspace_routing: bool,
}

impl HttpRelayAdapter {
    pub fn new(
        url: &str,
        headers: BTreeMap<String, String>,
        timeout_seconds: f64,
        workspace_routing: bool,
    ) -> Result<Self, String> {
        if !timeout_seconds.is_finite() || !(1.0..=MAX_FORWARD_TIMEOUT_S).contains(&timeout_seconds)
        {
            return Err(format!(
                "timeout_seconds must be between 1 and {MAX_FORWARD_TIMEOUT_S}"
            ));
        }
        let url = Url::parse(url).map_err(|_| "invalid backend url".to_string())?;
        let mut header_map = reqwest::header::HeaderMap::new();
        for (key, value) in headers {
            let name = reqwest::header::HeaderName::from_bytes(key.as_bytes())
                .map_err(|_| format!("invalid header name {key:?}"))?;
            let value = reqwest::header::HeaderValue::from_str(&value)
                .map_err(|_| format!("invalid value for header {key:?}"))?;
            header_map.insert(name, value);
        }
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs_f64(timeout_seconds))
            .build()
            .map_err(|error| format!("cannot build HTTP client: {error}"))?;
        Ok(Self {
            url,
            headers: header_map,
            client,
            workspace_routing,
        })
    }

    async fn roundtrip(
        &self,
        method: &str,
        params: Option<Value>,
        retryable: bool,
        workspace: Option<&str>,
    ) -> Result<Value, BackendError> {
        let attempts = if retryable { HTTP_RETRY_ATTEMPTS } else { 1 };
        let mut last_error = None;
        for attempt in 0..attempts {
            match self.roundtrip_once(method, params.clone(), workspace).await {
                Ok(result) => return Ok(result),
                Err(error) => {
                    if attempt + 1 >= attempts || !error.transient {
                        return Err(error);
                    }
                    last_error = Some(error);
                    tokio::time::sleep(HTTP_RETRY_DELAY).await;
                }
            }
        }
        Err(last_error.unwrap_or_else(|| BackendError::permanent(format!("{method} failed"))))
    }

    async fn roundtrip_once(
        &self,
        method: &str,
        params: Option<Value>,
        workspace: Option<&str>,
    ) -> Result<Value, BackendError> {
        let request_url = workspace
            .map(|workspace| url_with_workspace(&self.url, workspace))
            .unwrap_or_else(|| self.url.clone());
        let session = self.open_session(&request_url).await?;
        let _cleanup = self.session_cleanup(&request_url, &session);
        let outcome = async {
            let initialized = json!({"jsonrpc": "2.0", "method": "notifications/initialized"});
            self.post_json(&request_url, &initialized, Some(&session))
                .await?;
            let call = json!({
                "jsonrpc": "2.0",
                "id": 2,
                "method": method,
                "params": params.unwrap_or_else(|| json!({}))
            });
            let response = self.post_json(&request_url, &call, Some(&session)).await?;
            let body = response.bytes().await.map_err(|error| {
                BackendError::transient(format!("{method} response read failed: {error}"))
            })?;
            parse_rpc_response(&body, &json!(2), method)
        }
        .await;
        outcome
    }

    async fn open_session(&self, url: &Url) -> Result<UpstreamSession, BackendError> {
        let response = self
            .post_json(
                url,
                &json!({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "engram-gateway", "version": "0.0.1"}
                    }
                }),
                None,
            )
            .await?;
        let session_id = header_value(&response, "mcp-session-id");
        let session = UpstreamSession {
            id: session_id,
            protocol_version: "2024-11-05".to_string(),
        };
        let body = match response.bytes().await {
            Ok(body) => body,
            Err(error) => {
                self.close_session(url, &session).await;
                return Err(BackendError::transient(format!(
                    "initialize response read failed: {error}"
                )));
            }
        };
        match parse_rpc_response(&body, &json!(1), "initialize") {
            Ok(result) => {
                let protocol_version = result
                    .get("protocolVersion")
                    .and_then(Value::as_str)
                    .unwrap_or("2024-11-05")
                    .to_string();
                Ok(UpstreamSession {
                    id: session.id,
                    protocol_version,
                })
            }
            Err(error) => {
                self.close_session(url, &session).await;
                Err(error)
            }
        }
    }

    async fn close_session(&self, url: &Url, session: &UpstreamSession) {
        let Some(session_id) = session.id.as_deref() else {
            return;
        };
        let _ = self
            .client
            .delete(url.clone())
            .headers(self.request_headers(Some(session_id), Some(&session.protocol_version)))
            .send()
            .await;
    }

    fn session_cleanup(&self, url: &Url, session: &UpstreamSession) -> SessionCleanup {
        SessionCleanup {
            client: self.client.clone(),
            url: url.clone(),
            headers: self.request_headers(session.id.as_deref(), Some(&session.protocol_version)),
            session: session.clone(),
        }
    }

    async fn list_tools_once(&self) -> Result<Vec<Value>, BackendError> {
        let session = self.open_session(&self.url).await?;
        let _cleanup = self.session_cleanup(&self.url, &session);
        let outcome = async {
            self.post_json(
                &self.url,
                &json!({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                Some(&session),
            )
            .await?;
            let mut tools = Vec::new();
            let mut cursor: Option<String> = None;
            let mut seen = std::collections::HashSet::new();
            let mut pages = 0_usize;
            loop {
                pages += 1;
                if pages > MAX_TOOL_PAGES {
                    return Err(BackendError::permanent(
                        "tools/list exceeded the pagination limit",
                    ));
                }
                let params = cursor
                    .as_ref()
                    .map(|cursor| json!({"cursor": cursor}))
                    .unwrap_or_else(|| json!({}));
                let response = self
                    .post_json(
                        &self.url,
                        &json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params}),
                        Some(&session),
                    )
                    .await?;
                let body = response.bytes().await.map_err(|error| {
                    BackendError::transient(format!("tools/list response read failed: {error}"))
                })?;
                let result = parse_rpc_response(&body, &json!(2), "tools/list")?;
                tools.extend(
                    result
                        .get("tools")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default(),
                );
                let Some(next) = result
                    .get("nextCursor")
                    .and_then(Value::as_str)
                    .filter(|cursor| !cursor.is_empty())
                else {
                    break;
                };
                if !seen.insert(next.to_string()) {
                    return Err(BackendError::permanent(
                        "tools/list returned a repeated pagination cursor",
                    ));
                }
                cursor = Some(next.to_string());
            }
            Ok(tools)
        }
        .await;
        outcome
    }

    fn request_headers(
        &self,
        session_id: Option<&str>,
        protocol_version: Option<&str>,
    ) -> reqwest::header::HeaderMap {
        use reqwest::header::{HeaderName, HeaderValue, ACCEPT, CONTENT_TYPE};
        let mut headers = self.headers.clone();
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        headers.insert(
            ACCEPT,
            HeaderValue::from_static("application/json, text/event-stream"),
        );
        if let Some(session_id) = session_id.filter(|value| !value.is_empty()) {
            if let Ok(value) = HeaderValue::from_str(session_id) {
                headers.insert(HeaderName::from_static("mcp-session-id"), value);
            }
        }
        if let Some(protocol_version) = protocol_version {
            if let Ok(value) = HeaderValue::from_str(protocol_version) {
                headers.insert(HeaderName::from_static("mcp-protocol-version"), value);
            }
        }
        headers
    }

    async fn post_json(
        &self,
        url: &Url,
        payload: &Value,
        session: Option<&UpstreamSession>,
    ) -> Result<reqwest::Response, BackendError> {
        let response = self
            .client
            .post(url.clone())
            .headers(self.request_headers(
                session.and_then(|session| session.id.as_deref()),
                session.map(|session| session.protocol_version.as_str()),
            ))
            .json(payload)
            .send()
            .await
            .map_err(|error| {
                BackendError::transient(format!(
                    "HTTP transport failed for {}: {}",
                    redacted_url(url),
                    error.without_url()
                ))
            })?;
        if response.status().is_success() {
            return Ok(response);
        }
        let status = response.status().as_u16();
        let message = format!("HTTP {status} from {}", redacted_url(url));
        if matches!(status, 502..=504) {
            Err(BackendError::transient(message))
        } else {
            Err(BackendError::permanent(message))
        }
    }
}

#[derive(Clone)]
struct UpstreamSession {
    id: Option<String>,
    protocol_version: String,
}

/// Closes the upstream session even when the request future is dropped by
/// cancellation or a client disconnect. Drop cannot await, so the DELETE is
/// handed to the runtime.
struct SessionCleanup {
    client: reqwest::Client,
    url: Url,
    headers: reqwest::header::HeaderMap,
    session: UpstreamSession,
}

impl Drop for SessionCleanup {
    fn drop(&mut self) {
        let Some(id) = self.session.id.take() else {
            return;
        };
        if id.is_empty() {
            return;
        }
        let client = self.client.clone();
        let url = self.url.clone();
        let headers = self.headers.clone();
        tokio::spawn(async move {
            let _ = client.delete(url).headers(headers).send().await;
        });
    }
}

#[async_trait]
impl BackendAdapter for HttpRelayAdapter {
    async fn list_tools(&self) -> Result<Vec<Value>, BackendError> {
        let mut last_error = None;
        for attempt in 0..HTTP_RETRY_ATTEMPTS {
            match self.list_tools_once().await {
                Ok(tools) => return Ok(tools),
                Err(error) => {
                    if attempt + 1 >= HTTP_RETRY_ATTEMPTS || !error.transient {
                        return Err(error);
                    }
                    last_error = Some(error);
                    tokio::time::sleep(HTTP_RETRY_DELAY).await;
                }
            }
        }
        Err(last_error.unwrap_or_else(|| BackendError::permanent("tools/list failed")))
    }

    async fn call_tool(&self, name: &str, arguments: Value) -> Result<Value, BackendError> {
        self.roundtrip(
            "tools/call",
            Some(json!({"name": name, "arguments": arguments})),
            is_retryable_raw_tool(name),
            None,
        )
        .await
    }

    async fn call_tool_with_workspace(
        &self,
        name: &str,
        arguments: Value,
        workspace: &str,
    ) -> Result<Value, BackendError> {
        self.roundtrip(
            "tools/call",
            Some(json!({"name": name, "arguments": arguments})),
            is_retryable_raw_tool(name),
            Some(workspace),
        )
        .await
    }

    fn supports_workspace(&self) -> bool {
        self.workspace_routing
    }
}

fn redacted_url(url: &Url) -> String {
    let mut text = format!("{}://{}", url.scheme(), url.host_str().unwrap_or(""));
    if let Some(port) = url.port() {
        text.push(':');
        text.push_str(&port.to_string());
    }
    text.push_str(url.path());
    if url.query().is_some() {
        text.push_str("?…");
    }
    text
}

fn header_value(response: &reqwest::Response, name: &str) -> Option<String> {
    response
        .headers()
        .get(name)
        .and_then(|value| value.to_str().ok())
        .map(str::to_string)
}

fn url_with_workspace(base: &Url, workspace: &str) -> Url {
    let mut url = base.clone();
    let existing: Vec<(String, String)> = url
        .query_pairs()
        .filter(|(key, _)| key.as_ref() != "workspace")
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect();
    {
        let mut query = url.query_pairs_mut();
        query.clear();
        for (key, value) in existing {
            query.append_pair(&key, &value);
        }
        query.append_pair("workspace", workspace);
    }
    url
}

pub fn parse_sse_json(body: &[u8]) -> Result<Value, BackendError> {
    let text = String::from_utf8_lossy(body);
    let text = text.trim();
    let candidate = sse_events(text)
        .into_iter()
        .next()
        .unwrap_or_else(|| text.to_string());
    serde_json::from_str(&candidate)
        .map_err(|error| BackendError::permanent(format!("invalid SSE JSON: {error}")))
}

fn sse_events(text: &str) -> Vec<String> {
    let mut events = Vec::new();
    let mut current = Vec::new();
    for line in text.lines() {
        if line.trim().is_empty() {
            if !current.is_empty() {
                events.push(current.join("\n"));
                current.clear();
            }
        } else if let Some(data) = line.strip_prefix("data:") {
            current.push(data.trim_start().to_string());
        }
    }
    if !current.is_empty() {
        events.push(current.join("\n"));
    }
    events
}

fn parse_rpc_response(
    body: &[u8],
    expected_id: &Value,
    label: &str,
) -> Result<Value, BackendError> {
    let text = String::from_utf8_lossy(body);
    let text = text.trim();
    let mut candidates = if text.starts_with('{') || text.starts_with('[') {
        vec![text.to_string()]
    } else {
        sse_events(text)
    };
    if candidates.is_empty() {
        candidates.push(text.to_string());
    }
    for candidate in candidates {
        let Ok(payload) = serde_json::from_str::<Value>(&candidate) else {
            continue;
        };
        if payload.get("id") != Some(expected_id) {
            continue;
        }
        if let Some(error) = payload.get("error") {
            return Err(BackendError::permanent(format!("{label} failed: {error}")));
        }
        return Ok(payload.get("result").cloned().unwrap_or_else(|| json!({})));
    }
    Err(BackendError::permanent(format!(
        "{label} returned no JSON-RPC response for id {expected_id}"
    )))
}

pub struct ScopedHindsightAdapter {
    upstream: Arc<dyn BackendAdapter>,
    scope_tags: Vec<String>,
    allow_unscoped: bool,
    mental_model_ids: Vec<String>,
}

impl ScopedHindsightAdapter {
    pub fn new(
        upstream: Arc<dyn BackendAdapter>,
        scope_tags: Vec<String>,
        allow_unscoped: bool,
        mental_model_ids: Vec<String>,
    ) -> Self {
        Self {
            upstream,
            scope_tags,
            allow_unscoped,
            mental_model_ids,
        }
    }
}

#[async_trait]
impl BackendAdapter for ScopedHindsightAdapter {
    async fn list_tools(&self) -> Result<Vec<Value>, BackendError> {
        let mut tools = self.upstream.list_tools().await?;
        if self.mental_model_ids.is_empty() {
            tools.retain(|tool| {
                tool.get("name").and_then(Value::as_str) != Some("get_mental_model")
            });
        }
        Ok(tools)
    }

    async fn call_tool(&self, name: &str, arguments: Value) -> Result<Value, BackendError> {
        if name == "recall" {
            let Some(mut forwarded) = arguments.as_object().cloned() else {
                return Ok(tool_error("recall arguments must be an object"));
            };
            forwarded.remove("tag_groups");
            if !self.scope_tags.is_empty() {
                forwarded.insert("tags".into(), json!(self.scope_tags));
                forwarded.insert("tags_match".into(), json!("any_strict"));
            } else if self.allow_unscoped {
                forwarded.remove("tags");
                forwarded.remove("tags_match");
            } else {
                return Ok(tool_error(
                    "shared methodology recall requires a configured route scope",
                ));
            }
            return self
                .upstream
                .call_tool(name, Value::Object(forwarded))
                .await;
        }
        if name == "get_mental_model" {
            let model_id = arguments
                .get("mental_model_id")
                .and_then(Value::as_str)
                .unwrap_or("");
            if !self.mental_model_ids.iter().any(|id| id == model_id) {
                return Ok(tool_error("mental model is not authorized for this route"));
            }
        }
        self.upstream.call_tool(name, arguments).await
    }
}

pub struct ManualMentalModelAdapter {
    upstream: Arc<dyn BackendAdapter>,
}

impl ManualMentalModelAdapter {
    pub fn new(upstream: Arc<dyn BackendAdapter>) -> Self {
        Self { upstream }
    }
}

#[async_trait]
impl BackendAdapter for ManualMentalModelAdapter {
    async fn list_tools(&self) -> Result<Vec<Value>, BackendError> {
        Ok(vec![manual_mental_model_tool()])
    }

    async fn call_tool(&self, name: &str, arguments: Value) -> Result<Value, BackendError> {
        if name != "manual_mental_model" {
            return Ok(tool_error(format!("Unknown tool: {name:?}")));
        }
        let Some(arguments) = arguments.as_object() else {
            return Ok(tool_error("arguments must be an object"));
        };
        let Some(model_id) = arguments.get("model_id").and_then(Value::as_str) else {
            return Ok(tool_error(
                "model_id must be lowercase alphanumeric words separated by single hyphens",
            ));
        };
        if !valid_model_id(model_id) {
            return Ok(tool_error(
                "model_id must be lowercase alphanumeric words separated by single hyphens",
            ));
        }
        let Some(content) = arguments.get("content").and_then(Value::as_str) else {
            return Ok(tool_error("content must be a non-empty Markdown string"));
        };
        if content.trim().is_empty() {
            return Ok(tool_error("content must be a non-empty Markdown string"));
        }
        let tags = match arguments.get("tags") {
            None | Some(Value::Null) => None,
            Some(value) => match value.as_array() {
                Some(items) if items.iter().all(Value::is_string) => Some(value.clone()),
                _ => return Ok(tool_error("tags must be an array of strings")),
            },
        };
        let metadata = match arguments.get("metadata") {
            None | Some(Value::Null) => Map::new(),
            Some(Value::Object(metadata)) if metadata.values().all(Value::is_string) => {
                metadata.clone()
            }
            Some(_) => return Ok(tool_error("metadata must be an object of string values")),
        };
        let name = match optional_string(arguments, "name") {
            Ok(name) => name,
            Err(message) => return Ok(tool_error(message)),
        };
        let source_query = match optional_string(arguments, "source_query") {
            Ok(value) => value,
            Err(message) => return Ok(tool_error(message)),
        };

        let mut manual_metadata = metadata;
        manual_metadata.insert("source".into(), json!("manual_mental_model"));
        manual_metadata.insert("mental_model_id".into(), json!(model_id));
        if let Some(name) = name.filter(|name| !name.is_empty()) {
            manual_metadata.insert("name".into(), json!(name));
        }
        if let Some(source_query) = source_query.filter(|query| !query.is_empty()) {
            manual_metadata.insert("source_query".into(), json!(source_query));
        }
        let mut retain = Map::new();
        retain.insert("content".into(), json!(content));
        retain.insert("context".into(), json!("mental-models"));
        retain.insert(
            "document_id".into(),
            json!(format!("manual-mental-model-{model_id}")),
        );
        retain.insert("metadata".into(), Value::Object(manual_metadata));
        retain.insert("update_mode".into(), json!("replace"));
        if let Some(tags) = tags {
            retain.insert("tags".into(), tags);
        }
        self.upstream
            .call_tool("retain", Value::Object(retain))
            .await
    }
}

fn optional_string<'a>(
    arguments: &'a Map<String, Value>,
    key: &str,
) -> Result<Option<&'a str>, String> {
    match arguments.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value)),
        Some(_) => Err(format!("{key} must be a string")),
    }
}

fn valid_model_id(value: &str) -> bool {
    !value.is_empty()
        && value == value.to_lowercase()
        && value.split('-').all(|part| {
            !part.is_empty()
                && part
                    .chars()
                    .all(|character| character.is_ascii_alphanumeric())
        })
}

pub fn tool_error(message: impl Into<String>) -> Value {
    json!({
        "content": [{"type": "text", "text": message.into()}],
        "isError": true
    })
}

pub fn manual_mental_model_tool() -> Value {
    json!({
        "name": "manual_mental_model",
        "description": "Store caller-generated Markdown as a canonical replacement memory for a mental model. This does not refresh or overwrite Hindsight's pinned mental-model record; it replaces the previous manual document for the same model_id. The caller must provide the complete Markdown; the gateway does not invoke Hindsight's LLM-backed synthesis path.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": false,
            "required": ["model_id", "content"],
            "properties": {
                "model_id": {"type": "string", "minLength": 1, "description": "Stable lowercase mental-model ID, for example 'engram-architecture'."},
                "content": {"type": "string", "minLength": 1, "description": "Complete manually generated Markdown content."},
                "name": {"type": "string", "description": "Optional human-readable mental-model name kept as provenance metadata."},
                "source_query": {"type": "string", "description": "Optional query or scope used to manually produce the content."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional visibility tags for the canonical memory."},
                "metadata": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Optional additional string metadata."}
            }
        }
    })
}

/// Build ordered adapters for one project. Stdio/shadow specs are rejected
/// explicitly; manual writers are appended for docs/issues banks.
pub fn build_project_adapters(project: &str, spec: &ProjectSpec) -> Result<AdapterList, String> {
    let mut adapters: AdapterList = Vec::new();
    for (key, backend) in &spec.backends {
        match backend.kind {
            BackendKind::Http => {}
            BackendKind::Stdio | BackendKind::ShadowHttp => {
                return Err(format!(
                    "Backend {project:?}/{key:?} kind {:?} is not supported by the Rust HTTP gateway yet",
                    backend.kind.as_str()
                ));
            }
        }
        let url = backend
            .url
            .as_deref()
            .ok_or_else(|| format!("Backend {project:?}/{key:?} requires a url"))?;
        let http: Arc<dyn BackendAdapter> = Arc::new(
            HttpRelayAdapter::new(
                url,
                backend.headers.clone(),
                backend.timeout_seconds,
                backend.workspace_routing,
            )
            .map_err(|error| format!("Backend {project:?}/{key:?}: {error}"))?,
        );
        let adapter = if key == "hindsight" {
            Arc::new(ScopedHindsightAdapter::new(
                http,
                backend.scope_tags.clone(),
                backend.allow_unscoped,
                backend.mental_model_ids.clone(),
            )) as Arc<dyn BackendAdapter>
        } else {
            http
        };
        adapters.push((key.clone(), adapter));
    }
    for bank in ["docs", "issues"] {
        let Some(upstream) = adapters
            .iter()
            .find(|(key, _)| key == bank)
            .map(|(_, adapter)| Arc::clone(adapter))
        else {
            continue;
        };
        let key = format!("{bank}_manual");
        let manual = Arc::new(ManualMentalModelAdapter::new(upstream)) as Arc<dyn BackendAdapter>;
        if let Some(position) = adapters.iter().position(|(name, _)| name == &key) {
            adapters[position] = (key, manual);
        } else {
            adapters.push((key, manual));
        }
    }
    Ok(adapters)
}

/// Convenience for callers that need the routed-zvec rewrite in one place.
pub fn prepare_arguments(raw_name: &str, arguments: Value) -> Value {
    managed_zvec_arguments(raw_name, arguments)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[derive(Default)]
    struct Fake {
        tools: Vec<Value>,
        calls: Mutex<Vec<(String, Value)>>,
        result: Value,
    }

    #[async_trait]
    impl BackendAdapter for Fake {
        async fn list_tools(&self) -> Result<Vec<Value>, BackendError> {
            Ok(self.tools.clone())
        }

        async fn call_tool(&self, name: &str, arguments: Value) -> Result<Value, BackendError> {
            self.calls
                .lock()
                .unwrap()
                .push((name.to_string(), arguments));
            Ok(self.result.clone())
        }
    }

    #[test]
    fn sse_and_plain_json_parse() {
        let sse = parse_sse_json(b"event: message\ndata: {\"result\":{\"ok\":true}}\n\n").unwrap();
        assert_eq!(sse["result"]["ok"], true);
        let plain = parse_sse_json(b"{\"result\":{}}").unwrap();
        assert_eq!(plain["result"], json!({}));
    }

    #[test]
    fn workspace_query_replaces_without_losing_existing_params() {
        let url = Url::parse("http://127.0.0.1:9/mcp?token=x&workspace=old").unwrap();
        let updated = url_with_workspace(&url, "/new tree");
        let pairs: BTreeMap<String, String> = updated
            .query_pairs()
            .map(|(key, value)| (key.into_owned(), value.into_owned()))
            .collect();
        assert_eq!(pairs["token"], "x");
        assert_eq!(pairs["workspace"], "/new tree");
    }

    #[test]
    fn scoped_hindsight_overrides_recall_and_authorizes_models() {
        let upstream = Arc::new(Fake {
            tools: vec![
                json!({"name": "recall"}),
                json!({"name": "get_mental_model"}),
            ],
            result: json!({"ok": true}),
            ..Default::default()
        });
        let scoped = ScopedHindsightAdapter::new(
            upstream.clone(),
            vec!["project".into()],
            false,
            vec!["allowed".into()],
        );
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            scoped
                .call_tool(
                    "recall",
                    json!({"query": "x", "tags": ["other"], "tag_groups": [{"tags": ["x"]}]}),
                )
                .await
                .unwrap();
            let denied = scoped
                .call_tool("get_mental_model", json!({"mental_model_id": "other"}))
                .await
                .unwrap();
            assert_eq!(denied["isError"], true);
        });
        let calls = upstream.calls.lock().unwrap();
        assert_eq!(calls[0].0, "recall");
        assert_eq!(calls[0].1["tags"], json!(["project"]));
        assert_eq!(calls[0].1["tags_match"], "any_strict");
        assert!(calls[0].1.get("tag_groups").is_none());
    }

    #[test]
    fn scoped_hindsight_hides_unconfigured_mental_model_tool() {
        let upstream = Arc::new(Fake {
            tools: vec![
                json!({"name": "recall"}),
                json!({"name": "get_mental_model"}),
            ],
            ..Default::default()
        });
        let scoped = ScopedHindsightAdapter::new(upstream, vec![], true, vec![]);
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let tools = runtime.block_on(scoped.list_tools()).unwrap();
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0]["name"], "recall");
    }

    #[test]
    fn manual_writer_builds_the_exact_retain_payload() {
        let upstream = Arc::new(Fake {
            result: json!({"content": [], "isError": false}),
            ..Default::default()
        });
        let manual = ManualMentalModelAdapter::new(upstream.clone());
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            manual
                .call_tool(
                    "manual_mental_model",
                    json!({
                        "model_id": "engram-architecture",
                        "content": "# Architecture",
                        "tags": ["engram"],
                        "metadata": {"owner": "me"},
                        "name": "Architecture"
                    }),
                )
                .await
                .unwrap();
            let invalid = manual
                .call_tool(
                    "manual_mental_model",
                    json!({"model_id": "Bad", "content": "x"}),
                )
                .await
                .unwrap();
            assert_eq!(invalid["isError"], true);
        });
        let calls = upstream.calls.lock().unwrap();
        let (name, arguments) = &calls[0];
        assert_eq!(name, "retain");
        assert_eq!(
            arguments["document_id"],
            "manual-mental-model-engram-architecture"
        );
        assert_eq!(arguments["context"], "mental-models");
        assert_eq!(arguments["update_mode"], "replace");
        assert_eq!(arguments["metadata"]["source"], "manual_mental_model");
        assert_eq!(
            arguments["metadata"]["mental_model_id"],
            "engram-architecture"
        );
        assert_eq!(arguments["metadata"]["name"], "Architecture");
        assert_eq!(arguments["metadata"]["owner"], "me");
    }

    #[test]
    fn scoped_hindsight_rejects_non_object_recall_arguments() {
        let upstream = Arc::new(Fake::default());
        let scoped = ScopedHindsightAdapter::new(upstream, vec!["project".into()], false, vec![]);
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let result = runtime
            .block_on(scoped.call_tool("recall", json!([1, 2])))
            .unwrap();
        assert_eq!(result["isError"], true);
    }

    #[test]
    fn rejects_invalid_configured_headers_without_leaking_values() {
        let mut headers = BTreeMap::new();
        headers.insert("Authorization".into(), "bad\nvalue".into());
        let error = match HttpRelayAdapter::new("http://127.0.0.1:1/mcp", headers, 5.0, false) {
            Ok(_) => panic!("invalid header must be rejected"),
            Err(error) => error,
        };
        assert!(error.contains("Authorization"));
        assert!(!error.contains("bad"));
    }

    #[test]
    fn manual_adapter_replaces_an_explicit_backend_key() {
        let spec = crate::registry::ProjectSpec {
            backends: vec![
                (
                    "docs".into(),
                    crate::registry::BackendSpec {
                        kind: BackendKind::Http,
                        url: Some("http://127.0.0.1:1/mcp".into()),
                        headers: BTreeMap::new(),
                        timeout_seconds: 60.0,
                        workspace_routing: false,
                        allow_insecure_http: false,
                        scope_tags: vec![],
                        allow_unscoped: false,
                        mental_model_ids: vec![],
                        command: None,
                        args: vec![],
                        env: BTreeMap::new(),
                    },
                ),
                (
                    "docs_manual".into(),
                    crate::registry::BackendSpec {
                        kind: BackendKind::Http,
                        url: Some("http://127.0.0.1:2/mcp".into()),
                        headers: BTreeMap::new(),
                        timeout_seconds: 60.0,
                        workspace_routing: false,
                        allow_insecure_http: false,
                        scope_tags: vec![],
                        allow_unscoped: false,
                        mental_model_ids: vec![],
                        command: None,
                        args: vec![],
                        env: BTreeMap::new(),
                    },
                ),
            ],
        };
        let adapters = build_project_adapters("demo", &spec).unwrap();
        assert_eq!(
            adapters
                .iter()
                .filter(|(key, _)| key == "docs_manual")
                .count(),
            1
        );
        let manual = adapters
            .iter()
            .find(|(key, _)| key == "docs_manual")
            .map(|(_, adapter)| Arc::clone(adapter))
            .unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let tools = runtime.block_on(manual.list_tools()).unwrap();
        assert_eq!(tools[0]["name"], "manual_mental_model");
    }

    #[test]
    fn rejects_stdio_and_shadow_at_build_time() {
        let spec = ProjectSpec {
            backends: vec![(
                "serena".into(),
                crate::registry::BackendSpec {
                    kind: BackendKind::Stdio,
                    url: None,
                    headers: BTreeMap::new(),
                    timeout_seconds: 60.0,
                    workspace_routing: false,
                    allow_insecure_http: false,
                    scope_tags: vec![],
                    allow_unscoped: false,
                    mental_model_ids: vec![],
                    command: Some("/opt/serena".into()),
                    args: vec![],
                    env: BTreeMap::new(),
                },
            )],
        };
        let error = match build_project_adapters("demo", &spec) {
            Ok(_) => panic!("stdio backend must be rejected"),
            Err(error) => error,
        };
        assert!(error.contains("not supported by the Rust HTTP gateway"));
    }
}
