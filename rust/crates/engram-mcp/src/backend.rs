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

pub struct HttpRelayAdapter {
    url: Url,
    headers: BTreeMap<String, String>,
    client: reqwest::Client,
}

impl HttpRelayAdapter {
    pub fn new(
        url: &str,
        headers: BTreeMap<String, String>,
        timeout_seconds: f64,
    ) -> Result<Self, String> {
        if !timeout_seconds.is_finite() || !(1.0..=MAX_FORWARD_TIMEOUT_S).contains(&timeout_seconds)
        {
            return Err(format!(
                "timeout_seconds must be between 1 and {MAX_FORWARD_TIMEOUT_S}"
            ));
        }
        let url = Url::parse(url).map_err(|error| format!("invalid backend url: {error}"))?;
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs_f64(timeout_seconds))
            .build()
            .map_err(|error| format!("cannot build HTTP client: {error}"))?;
        Ok(Self {
            url,
            headers,
            client,
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
        let mut session_id: Option<String> = None;
        let result = async {
            let init = self
                .post_json(
                    &request_url,
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
            session_id = header_value(&init, "mcp-session-id");

            let initialized = json!({"jsonrpc": "2.0", "method": "notifications/initialized"});
            self.post_json(&request_url, &initialized, session_id.as_deref())
                .await?;

            let call = json!({
                "jsonrpc": "2.0",
                "id": 2,
                "method": method,
                "params": params.unwrap_or_else(|| json!({}))
            });
            let response = self
                .post_json(&request_url, &call, session_id.as_deref())
                .await?;
            let payload = parse_sse_json(&response.bytes().await.map_err(|error| {
                BackendError::transient(format!("{method} response read failed: {error}"))
            })?)?;
            if let Some(error) = payload.get("error") {
                return Err(BackendError::permanent(format!("{method} failed: {error}")));
            }
            Ok(payload.get("result").cloned().unwrap_or_else(|| json!({})))
        }
        .await;

        if let Some(session_id) = session_id {
            let _ = self
                .client
                .delete(request_url)
                .headers(self.request_headers(Some(&session_id)))
                .send()
                .await;
        }
        result
    }

    fn request_headers(&self, session_id: Option<&str>) -> reqwest::header::HeaderMap {
        use reqwest::header::{HeaderMap, HeaderName, HeaderValue, ACCEPT, CONTENT_TYPE};
        let mut headers = HeaderMap::new();
        for (key, value) in &self.headers {
            if let (Ok(name), Ok(value)) = (
                HeaderName::from_bytes(key.as_bytes()),
                HeaderValue::from_str(value),
            ) {
                headers.insert(name, value);
            }
        }
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        headers.insert(
            ACCEPT,
            HeaderValue::from_static("application/json, text/event-stream"),
        );
        if let Some(session_id) = session_id {
            if let Ok(value) = HeaderValue::from_str(session_id) {
                headers.insert(HeaderName::from_static("mcp-session-id"), value);
            }
        }
        headers
    }

    async fn post_json(
        &self,
        url: &Url,
        payload: &Value,
        session_id: Option<&str>,
    ) -> Result<reqwest::Response, BackendError> {
        let response = self
            .client
            .post(url.clone())
            .headers(self.request_headers(session_id))
            .json(payload)
            .send()
            .await
            .map_err(|error| BackendError::transient(format!("HTTP transport failed: {error}")))?;
        if response.status().is_success() {
            return Ok(response);
        }
        let status = response.status().as_u16();
        let message = format!("HTTP {status} from {url}");
        if matches!(status, 502..=504) {
            Err(BackendError::transient(message))
        } else {
            Err(BackendError::permanent(message))
        }
    }
}

#[async_trait]
impl BackendAdapter for HttpRelayAdapter {
    async fn list_tools(&self) -> Result<Vec<Value>, BackendError> {
        let result = self.roundtrip("tools/list", None, true, None).await?;
        Ok(result
            .get("tools")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
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
        true
    }
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
    for line in text.lines() {
        if let Some(payload) = line.strip_prefix("data:") {
            let payload = payload.trim();
            if !payload.is_empty() {
                return serde_json::from_str(payload).map_err(|error| {
                    BackendError::permanent(format!("invalid SSE JSON: {error}"))
                });
            }
        }
    }
    serde_json::from_str(text)
        .map_err(|error| BackendError::permanent(format!("invalid JSON response: {error}")))
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
            let mut forwarded = arguments.as_object().cloned().unwrap_or_default();
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
            HttpRelayAdapter::new(url, backend.headers.clone(), backend.timeout_seconds)
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
        if let Some((_, upstream)) = adapters.iter().find(|(key, _)| key == bank) {
            adapters.push((
                format!("{bank}_manual"),
                Arc::new(ManualMentalModelAdapter::new(Arc::clone(upstream))),
            ));
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
