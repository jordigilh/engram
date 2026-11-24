//! End-to-end routing tests: real HTTP adapter against a mock MCP backend.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use axum::body::{to_bytes, Body};
use axum::extract::{Query, State};
use axum::http::{HeaderMap, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use engram_mcp::backend::{BackendAdapter, HttpRelayAdapter};
use engram_mcp::server::tool_definition;
use engram_mcp::{
    build_project_adapters, router, router_with_options, MetricsLog, OriginPolicy, Project,
    Registry, SessionPolicy,
};
use serde_json::{json, Value};
use tower::ServiceExt;

#[derive(Clone)]
struct MockState {
    tools: Vec<Value>,
    second_page: Vec<Value>,
    paged: bool,
    call_result: Value,
    list_fail_first: usize,
    list_always_fail: Arc<std::sync::atomic::AtomicBool>,
    call_fail_first: usize,
    call_fail_status: u16,
    call_jsonrpc_error: bool,
    call_no_response: bool,
    call_typed_busy: bool,
    call_typed_unbound: bool,
    init_jsonrpc_error: bool,
    call_delay_ms: u64,
    list_pause_at: Option<usize>,
    list_paused: Arc<tokio::sync::Semaphore>,
    list_release: Arc<tokio::sync::Semaphore>,
    sse_notification_first: bool,
    list_attempts: Arc<AtomicUsize>,
    call_attempts: Arc<AtomicUsize>,
    records: Arc<Mutex<Vec<Record>>>,
}

#[derive(Clone, Debug)]
struct Record {
    method: String,
    tool: Option<String>,
    arguments: Option<Value>,
    session: Option<String>,
    accept: Option<String>,
    protocol_version: Option<String>,
    workspace: Option<String>,
    init: Option<Value>,
}

#[derive(Default)]
struct MockConfig {
    tools: Vec<Value>,
    second_page: Vec<Value>,
    paged: bool,
    call_result: Value,
    list_fail_first: usize,
    list_always_fail: bool,
    call_fail_first: usize,
    call_fail_status: Option<u16>,
    call_jsonrpc_error: bool,
    call_no_response: bool,
    call_typed_busy: bool,
    call_typed_unbound: bool,
    init_jsonrpc_error: bool,
    call_delay_ms: u64,
    list_pause_at: Option<usize>,
    sse_notification_first: bool,
}

async fn spawn_mock(config: MockConfig) -> (String, MockState) {
    let state = MockState {
        tools: config.tools,
        second_page: config.second_page,
        paged: config.paged,
        call_result: config.call_result,
        list_fail_first: config.list_fail_first,
        list_always_fail: Arc::new(std::sync::atomic::AtomicBool::new(config.list_always_fail)),
        call_fail_first: config.call_fail_first,
        call_fail_status: config.call_fail_status.unwrap_or(503),
        call_jsonrpc_error: config.call_jsonrpc_error,
        call_no_response: config.call_no_response,
        call_typed_busy: config.call_typed_busy,
        init_jsonrpc_error: config.init_jsonrpc_error,
        call_delay_ms: config.call_delay_ms,
        list_pause_at: config.list_pause_at,
        list_paused: Arc::new(tokio::sync::Semaphore::new(0)),
        list_release: Arc::new(tokio::sync::Semaphore::new(0)),
        sse_notification_first: config.sse_notification_first,
        call_typed_unbound: config.call_typed_unbound,
        list_attempts: Arc::new(AtomicUsize::new(0)),
        call_attempts: Arc::new(AtomicUsize::new(0)),
        records: Arc::new(Mutex::new(Vec::new())),
    };
    let app = Router::new()
        .route("/mcp", post(mock_post).delete(mock_delete))
        .with_state(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("ephemeral listener");
    let address = listener.local_addr().expect("address");
    let serve_state = state.clone();
    tokio::spawn(async move {
        axum::serve(listener, app).await.expect("mock server");
        drop(serve_state);
    });
    (format!("http://{address}/mcp?token=x"), state)
}

async fn mock_post(
    State(state): State<MockState>,
    Query(query): Query<BTreeMap<String, String>>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    let method = body
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let params = body.get("params");
    let tool = params
        .and_then(|params| params.get("name"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let arguments = params.and_then(|params| params.get("arguments")).cloned();
    state.records.lock().unwrap().push(Record {
        method: method.clone(),
        tool,
        arguments,
        session: headers
            .get("mcp-session-id")
            .and_then(|value| value.to_str().ok())
            .map(str::to_string),
        accept: headers
            .get("accept")
            .and_then(|value| value.to_str().ok())
            .map(str::to_string),
        protocol_version: headers
            .get("mcp-protocol-version")
            .and_then(|value| value.to_str().ok())
            .map(str::to_string),
        workspace: query.get("workspace").cloned(),
        init: (method == "initialize").then(|| body.clone()),
    });

    match method.as_str() {
        "initialize" => {
            if state.init_jsonrpc_error {
                let mut response = Json(json!({
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32000, "message": "initialize rejected"}
                }))
                .into_response();
                response
                    .headers_mut()
                    .insert("mcp-session-id", "sess-1".parse().unwrap());
                return response;
            }
            let mut response = Json(json!({
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
            }))
            .into_response();
            response
                .headers_mut()
                .insert("mcp-session-id", "sess-1".parse().unwrap());
            response
        }
        "notifications/initialized" => StatusCode::ACCEPTED.into_response(),
        "tools/list" => {
            let attempt = state.list_attempts.fetch_add(1, Ordering::SeqCst) + 1;
            if state.list_pause_at == Some(attempt) {
                state.list_paused.add_permits(1);
                let _permit = state.list_release.acquire().await;
            }
            if state.list_always_fail.load(Ordering::SeqCst) || attempt <= state.list_fail_first {
                return StatusCode::SERVICE_UNAVAILABLE.into_response();
            }
            let cursor = body
                .get("params")
                .and_then(|params| params.get("cursor"))
                .and_then(Value::as_str);
            let (tools, next_cursor) = if state.paged {
                match cursor {
                    None => (state.tools.clone(), Some("page-2")),
                    Some("page-2") => (state.second_page.clone(), None),
                    Some(_) => (Vec::new(), None),
                }
            } else {
                (state.tools.clone(), None)
            };
            let mut result = json!({"tools": tools});
            if let Some(next) = next_cursor {
                result["nextCursor"] = json!(next);
            }
            let payload = json!({"jsonrpc": "2.0", "id": body.get("id"), "result": result});
            (
                [(axum::http::header::CONTENT_TYPE, "text/event-stream")],
                format!("event: message\ndata: {payload}\n\n"),
            )
                .into_response()
        }
        "tools/call" => {
            let attempt = state.call_attempts.fetch_add(1, Ordering::SeqCst) + 1;
            if state.call_no_response {
                return Json(json!({
                    "jsonrpc": "2.0",
                    "method": "notifications/progress"
                }))
                .into_response();
            }
            if state.call_typed_unbound {
                return (
                    StatusCode::CONFLICT,
                    [("x-engram-error", "workspace_unbound")],
                    Json(json!({
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {
                            "code": -32002,
                            "message": "workspace_unbound",
                            "data": {
                                "kind": "workspace_unbound",
                                "retryable": false
                            }
                        }
                    })),
                )
                    .into_response();
            }
            if state.call_typed_busy {
                return (
                    StatusCode::SERVICE_UNAVAILABLE,
                    [
                        ("x-engram-error", "workspace_flushing"),
                        ("retry-after", "2"),
                    ],
                    Json(json!({
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {
                            "code": -32002,
                            "message": "workspace_flushing",
                            "data": {
                                "kind": "workspace_flushing",
                                "retryable": true,
                                "workspace": "/work",
                                "epoch": 4,
                                "retry_after_ms": 2000
                            }
                        }
                    })),
                )
                    .into_response();
            }
            if state.call_jsonrpc_error {
                return Json(json!({
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32000, "message": "backend rejected"}
                }))
                .into_response();
            }
            if attempt <= state.call_fail_first {
                return StatusCode::from_u16(state.call_fail_status)
                    .unwrap_or(StatusCode::SERVICE_UNAVAILABLE)
                    .into_response();
            }
            if state.call_delay_ms > 0 {
                tokio::time::sleep(std::time::Duration::from_millis(state.call_delay_ms)).await;
            }
            let response =
                json!({"jsonrpc": "2.0", "id": body.get("id"), "result": state.call_result});
            if state.sse_notification_first {
                return (
                    [(axum::http::header::CONTENT_TYPE, "text/event-stream")],
                    format!(
                        "event: message\ndata: {{\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}}\n\nevent: message\ndata: {response}\n\n"
                    ),
                )
                    .into_response();
            }
            Json(response).into_response()
        }
        _ => StatusCode::BAD_REQUEST.into_response(),
    }
}

async fn mock_delete() -> StatusCode {
    StatusCode::OK
}

fn tool(name: &str) -> Value {
    tool_definition(name, name)
}

async fn call(app: &Router, uri: &str, session: Option<&str>, body: Value) -> (StatusCode, String) {
    let mut builder = Request::builder().method("POST").uri(uri);
    if let Some(session) = session {
        builder = builder.header("mcp-session-id", session);
    }
    let response = app
        .clone()
        .oneshot(builder.body(Body::from(body.to_string())).expect("request"))
        .await
        .expect("router responds");
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 64 * 1024)
        .await
        .expect("bounded body");
    (status, String::from_utf8_lossy(&bytes).to_string())
}

fn sse_data(body: &str) -> Value {
    for line in body.lines() {
        if let Some(payload) = line.strip_prefix("data: ") {
            return serde_json::from_str(payload).expect("SSE JSON");
        }
    }
    panic!("no SSE data in {body:?}");
}

async fn initialize(app: &Router, project: &str) -> String {
    let (status, body) = call(
        app,
        &format!("/mcp/{project}"),
        None,
        json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["result"]["protocolVersion"], "2024-11-05");
    // The helper re-issues initialize to read the header; use a direct request.
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/mcp/{project}"))
                .body(Body::from(
                    json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).to_string(),
                ))
                .expect("request"),
        )
        .await
        .expect("router responds");
    response
        .headers()
        .get("mcp-session-id")
        .expect("session")
        .to_str()
        .expect("ascii")
        .to_string()
}

#[tokio::test]
async fn http_adapter_runs_one_shot_session_and_forwards_workspace() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [{"type": "text", "text": "ok"}], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, true).expect("adapter");

    assert_eq!(adapter.list_tools().await.unwrap()[0]["name"], "recall");
    let result = adapter
        .call_tool("recall", json!({"query": "x"}))
        .await
        .unwrap();
    assert_eq!(result["isError"], false);

    let records = mock.records.lock().unwrap().clone();
    let methods: Vec<&str> = records
        .iter()
        .map(|record| record.method.as_str())
        .collect();
    assert_eq!(
        methods,
        vec![
            "initialize",
            "notifications/initialized",
            "tools/list",
            "initialize",
            "notifications/initialized",
            "tools/call"
        ]
    );
    assert_eq!(
        records[0].init.as_ref().unwrap()["params"]["protocolVersion"],
        "2024-11-05"
    );
    assert_eq!(
        records[2].accept.as_deref(),
        Some("application/json, text/event-stream")
    );
    assert_eq!(records[0].session, None);
    assert_eq!(records[2].session.as_deref(), Some("sess-1"));
    assert_eq!(records[5].tool.as_deref(), Some("recall"));
    assert!(records.iter().all(|record| record.workspace.is_none()));
    assert_eq!(mock.records.lock().unwrap().len(), 6);

    // Workspace appears on every roundtrip of a workspace-scoped call.
    let _ = adapter
        .call_tool_with_workspace("recall", json!({}), "/worktree/a")
        .await
        .unwrap();
    for record in mock.records.lock().unwrap().iter().skip(6) {
        assert_eq!(record.workspace.as_deref(), Some("/worktree/a"));
    }
}

#[tokio::test]
async fn reads_retry_transient_failures_but_writes_do_not() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        list_fail_first: 1,
        call_fail_first: 1,
        ..Default::default()
    })
    .await;
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).expect("adapter");

    assert_eq!(adapter.list_tools().await.unwrap()[0]["name"], "recall");
    assert_eq!(mock.list_attempts.load(Ordering::SeqCst), 2);
    assert_eq!(
        adapter.call_tool("recall", json!({})).await.unwrap()["isError"],
        false
    );
    assert_eq!(mock.call_attempts.load(Ordering::SeqCst), 2);

    let (url, mock) = spawn_mock(MockConfig {
        call_result: json!({"content": [], "isError": false}),
        call_fail_first: 1,
        ..Default::default()
    })
    .await;
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).expect("adapter");
    let error = adapter
        .call_tool("retain", json!({"content": "x"}))
        .await
        .expect_err("write must not retry");
    assert!(error.message.contains("HTTP 503"));
    assert_eq!(mock.call_attempts.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn gateway_aggregates_tools_and_routes_prefixed_calls() {
    let (docs_url, docs) = spawn_mock(MockConfig {
        tools: vec![tool("recall"), tool("retain")],
        call_result: json!({"content": [{"type": "text", "text": "found"}], "isError": false}),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.alpha.backends.docs]
kind = "http"
url = "{docs_url}"
"#
    ))
    .expect("registry");
    let spec = registry.get("alpha").unwrap();
    let adapters = build_project_adapters("alpha", spec).expect("adapters");
    let project = Project::with_backends("alpha", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    )
    .await;
    let names: Vec<String> = sse_data(&body)["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap().to_string())
        .collect();
    assert!(names.contains(&"docs_recall".to_string()));
    assert!(names.contains(&"docs_retain".to_string()));
    assert!(names.contains(&"docs_manual_mental_model".to_string()));

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {"query": "x"}}
        }),
    )
    .await;
    assert_eq!(sse_data(&body)["result"]["isError"], false);
    let records = docs.records.lock().unwrap();
    assert!(records
        .iter()
        .any(|record| record.method == "tools/call" && record.tool.as_deref() == Some("recall")));
}

#[tokio::test]
async fn tools_call_appends_a_gateway_metric_line() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [{"type": "text", "text": "héllo"}], "isError": false}),
        ..Default::default()
    })
    .await;
    let dir = std::env::temp_dir().join(format!("engram-metrics-ok-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let log = Arc::new(MetricsLog::new(dir.join("gateway-calls.jsonl")));
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]).with_metrics(log);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {"query": "x"}}
        }),
    )
    .await;
    assert_eq!(sse_data(&body)["result"]["isError"], false);

    let text = std::fs::read_to_string(dir.join("gateway-calls.jsonl")).expect("metrics log");
    let entry: Value = serde_json::from_str(text.trim()).expect("metric json");
    assert_eq!(entry["project"], "alpha");
    assert_eq!(entry["backend"], "docs");
    assert_eq!(entry["tool"], "docs_recall");
    assert_eq!(entry["is_error"], false);
    assert_eq!(entry["result_chars"], 5);
    assert!(entry["est_tokens"].as_u64().unwrap() > 0);
    assert_eq!(entry["ts"].as_str().unwrap().len(), 19);
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn backend_failure_appends_a_zeroed_error_metric() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_fail_first: 5,
        call_fail_status: Some(500),
        ..Default::default()
    })
    .await;
    let dir = std::env::temp_dir().join(format!("engram-metrics-err-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let log = Arc::new(MetricsLog::new(dir.join("gateway-calls.jsonl")));
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]).with_metrics(log);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {"query": "x"}}
        }),
    )
    .await;
    let value = sse_data(&body);
    let text = value["result"]["content"][0]["text"]
        .as_str()
        .unwrap_or_default();
    assert!(
        text.contains("backend 'docs' failed:"),
        "client-visible error text: {text:?}"
    );

    let text = std::fs::read_to_string(dir.join("gateway-calls.jsonl")).expect("metrics log");
    let entry: Value = serde_json::from_str(text.trim()).expect("metric json");
    assert_eq!(entry["is_error"], true);
    assert_eq!(entry["result_chars"], 0);
    assert_eq!(entry["est_tokens"], 0);
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn unknown_tool_does_not_write_a_metric_line() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let dir = std::env::temp_dir().join(format!("engram-metrics-unknown-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let log = Arc::new(MetricsLog::new(dir.join("gateway-calls.jsonl")));
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]).with_metrics(log);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "never_existed", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(sse_data(&body)["result"]["isError"], true);
    assert!(
        !dir.join("gateway-calls.jsonl").exists(),
        "an unknown tool never reached a backend and must not be logged"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn tools_call_works_immediately_after_prewarm() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(sse_data(&body)["result"]["isError"], false);
}

#[tokio::test]
async fn search_for_pattern_alias_translates_and_requires_workspace() {
    let (url, zg) = spawn_mock(MockConfig {
        tools: vec![tool("zvec_grep_rg"), tool("zvec_grep_search")],
        call_result: json!({"content": [{"type": "text", "text": "hit"}], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("zg".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}),
    )
    .await;
    let names: Vec<String> = sse_data(&body)["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap().to_string())
        .collect();
    assert!(names.contains(&"search_for_pattern".to_string()));

    let (_, body) = call(
        &app,
        "/mcp/alpha?workspace=%2Fworktree",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "search_for_pattern",
                "arguments": {"substring_pattern": "Syncer", "context_lines_after": 2}
            }
        }),
    )
    .await;
    assert_eq!(sse_data(&body)["result"]["isError"], false);
    {
        let records = zg.records.lock().unwrap();
        let routed = records
            .iter()
            .find(|record| record.method == "tools/call")
            .expect("routed call");
        assert_eq!(routed.tool.as_deref(), Some("zvec_grep_rg"));
        assert_eq!(routed.arguments.as_ref().unwrap()["root"], "/worktree");
        assert!(routed.arguments.as_ref().unwrap()["command"]
            .as_str()
            .unwrap()
            .contains("Syncer"));
        assert!(routed
            .arguments
            .as_ref()
            .unwrap()
            .get("freshness")
            .is_none());
    }

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "search_for_pattern", "arguments": {"substring_pattern": "x"}}
        }),
    )
    .await;
    assert_eq!(sse_data(&body)["result"]["isError"], true);
    assert!(sse_data(&body)["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("requires the request workspace"));
}

#[tokio::test]
async fn managed_zvec_search_gets_freshness_controls() {
    let (url, zg) = spawn_mock(MockConfig {
        tools: vec![tool("zvec_grep_search")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("zg".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, _) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "zvec_grep_search", "arguments": {"query": "q"}}
        }),
    )
    .await;
    let records = zg.records.lock().unwrap();
    let routed = records
        .iter()
        .find(|record| record.method == "tools/call")
        .expect("routed call");
    assert_eq!(routed.arguments.as_ref().unwrap()["autoUpdate"], true);
    assert_eq!(
        routed.arguments.as_ref().unwrap()["freshness"],
        "wait_for_fresh"
    );
}

#[tokio::test]
async fn permanent_http_and_jsonrpc_errors_are_not_retried() {
    let (url, mock) = spawn_mock(MockConfig {
        call_result: json!({"content": [], "isError": false}),
        call_fail_first: 1,
        call_fail_status: Some(500),
        ..Default::default()
    })
    .await;
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).expect("adapter");
    let error = adapter
        .call_tool("recall", json!({}))
        .await
        .expect_err("500 is permanent");
    assert!(error.message.contains("HTTP 500"));
    assert!(!error.message.contains("token="));
    assert!(error.message.contains("?…"));
    assert_eq!(mock.call_attempts.load(Ordering::SeqCst), 1);

    let (url, mock) = spawn_mock(MockConfig {
        call_jsonrpc_error: true,
        ..Default::default()
    })
    .await;
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).expect("adapter");
    let error = adapter
        .call_tool("recall", json!({}))
        .await
        .expect_err("JSON-RPC error is permanent");
    assert!(error.message.contains("tools/call failed"));
    assert_eq!(mock.call_attempts.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn tools_list_replaces_the_catalog_after_total_failure() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({"jsonrpc": "2.0", "id": 11, "method": "tools/list"}),
    )
    .await;
    assert!(!sse_data(&body)["result"]["tools"]
        .as_array()
        .unwrap()
        .is_empty());

    mock.list_always_fail.store(true, Ordering::SeqCst);
    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({"jsonrpc": "2.0", "id": 12, "method": "tools/list"}),
    )
    .await;
    assert!(sse_data(&body)["result"]["tools"]
        .as_array()
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn legacy_endpoint_host_passes_raw_names_through() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("docs_recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.legacy]
endpoint = "{url}"
"#
    ))
    .expect("registry");
    let adapters = build_project_adapters("legacy", registry.get("legacy").unwrap()).unwrap();
    let project = Project::with_backends("legacy", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "legacy").await;

    let (_, body) = call(
        &app,
        "/mcp/legacy",
        Some(&session),
        json!({"jsonrpc": "2.0", "id": 13, "method": "tools/list"}),
    )
    .await;
    let names: Vec<String> = sse_data(&body)["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(names, vec!["docs_recall"]);

    let (_, body) = call(
        &app,
        "/mcp/legacy",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(sse_data(&body)["result"]["isError"], false);
    let records = mock.records.lock().unwrap();
    assert!(records.iter().any(
        |record| record.method == "tools/call" && record.tool.as_deref() == Some("docs_recall")
    ));
}

#[tokio::test]
async fn partial_backend_failure_keeps_survivors_and_projects_are_isolated() {
    let (docs_url, _) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let dead_adapter: Arc<dyn BackendAdapter> = Arc::new(
        HttpRelayAdapter::new("http://127.0.0.1:1/mcp", BTreeMap::new(), 1.0, false).unwrap(),
    );
    let live: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&docs_url, BTreeMap::new(), 5.0, false).unwrap());
    let alpha = Project::with_backends(
        "alpha",
        vec![
            ("docs".into(), Arc::clone(&live)),
            ("code".into(), dead_adapter),
        ],
    );
    let beta = Project::with_backends("beta", vec![("docs".into(), live)]);
    alpha.prewarm().await;
    beta.prewarm().await;
    let app = router([alpha, beta]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}),
    )
    .await;
    let names: Vec<String> = sse_data(&body)["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap().to_string())
        .collect();
    assert!(names.contains(&"docs_recall".to_string()));
    assert!(!names.contains(&"zvec_grep_search".to_string()));

    let (status, _) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "ghost", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn sends_negotiated_version_and_follows_tools_pagination() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("page_one")],
        second_page: vec![tool("page_two")],
        paged: true,
        ..Default::default()
    })
    .await;
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).expect("adapter");
    let tools = adapter.list_tools().await.unwrap();
    let names: Vec<&str> = tools
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["page_one", "page_two"]);

    let records = mock.records.lock().unwrap();
    assert_eq!(records[0].protocol_version, None);
    for record in records.iter().skip(1) {
        assert_eq!(record.protocol_version.as_deref(), Some("2024-11-05"));
    }
    assert_eq!(
        records
            .iter()
            .filter(|record| record.method == "tools/list")
            .count(),
        2
    );
}

#[tokio::test]
async fn sse_notification_before_response_is_skipped_by_rpc_id() {
    let (url, _mock) = spawn_mock(MockConfig {
        call_result: json!({"content": [{"type": "text", "text": "ok"}], "isError": false}),
        sse_notification_first: true,
        ..Default::default()
    })
    .await;
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).expect("adapter");
    let result = adapter.call_tool("recall", json!({})).await.unwrap();
    assert_eq!(result["isError"], false);
    assert_eq!(result["content"][0]["text"], "ok");
}

#[tokio::test]
async fn initialize_jsonrpc_error_stops_the_handshake() {
    let (url, mock) = spawn_mock(MockConfig {
        init_jsonrpc_error: true,
        ..Default::default()
    })
    .await;
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).expect("adapter");
    let error = adapter.list_tools().await.expect_err("initialize rejects");
    assert!(error.message.contains("initialize failed"));
    assert_eq!(mock.list_attempts.load(Ordering::SeqCst), 0);
    assert!(mock
        .records
        .lock()
        .unwrap()
        .iter()
        .all(|record| record.method == "initialize"));
}

#[tokio::test]
async fn workspace_routing_flag_controls_forwarding() {
    let (editor_url, editor) = spawn_mock(MockConfig {
        tools: vec![tool("find_symbol")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.editor.backends.editor]
kind = "http"
url = "{editor_url}"
workspace_routing = true
"#
    ))
    .unwrap();
    let adapters = build_project_adapters("editor", registry.get("editor").unwrap()).unwrap();
    let project = Project::with_backends("editor", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "editor").await;
    let (_, _) = call(
        &app,
        "/mcp/editor?workspace=%2Fwork",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "find_symbol", "arguments": {}}
        }),
    )
    .await;
    assert!(editor
        .records
        .lock()
        .unwrap()
        .iter()
        .filter(|record| record.method == "tools/call")
        .all(|record| record.workspace.as_deref() == Some("/work")));

    let (serena_url, serena) = spawn_mock(MockConfig {
        tools: vec![tool("find_symbol")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.fixed.backends.serena]
kind = "http"
url = "{serena_url}"
workspace_routing = false
"#
    ))
    .unwrap();
    let adapters = build_project_adapters("fixed", registry.get("fixed").unwrap()).unwrap();
    let project = Project::with_backends("fixed", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "fixed").await;
    let (_, _) = call(
        &app,
        "/mcp/fixed?workspace=%2Fwork",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {"name": "find_symbol", "arguments": {}}
        }),
    )
    .await;
    assert!(serena
        .records
        .lock()
        .unwrap()
        .iter()
        .filter(|record| record.method == "tools/call")
        .all(|record| record.workspace.is_none()));
}

#[tokio::test]
async fn in_flight_cancellation_returns_cancelled() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        call_delay_ms: 500,
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let call_app = app.clone();
    let call_session = session.clone();
    let started = tokio::time::Instant::now();
    let pending = tokio::spawn(async move {
        call(
            &call_app,
            "/mcp/alpha",
            Some(&call_session),
            json!({
                "jsonrpc": "2.0",
                "id": 77,
                "method": "tools/call",
                "params": {"name": "docs_recall", "arguments": {}}
            }),
        )
        .await
    });
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 77}
        }),
    )
    .await;
    let (_, body) = pending.await.expect("call completes");
    assert_eq!(sse_data(&body)["error"]["code"], -32800);
    assert!(started.elapsed() < std::time::Duration::from_millis(400));
}

#[tokio::test]
async fn projects_list_and_call_only_their_own_tools() {
    let (alpha_url, alpha_mock) = spawn_mock(MockConfig {
        tools: vec![tool("alpha_search")],
        call_result: json!({"content": [{"type": "text", "text": "alpha"}], "isError": false}),
        ..Default::default()
    })
    .await;
    let (beta_url, beta_mock) = spawn_mock(MockConfig {
        tools: vec![tool("beta_search")],
        call_result: json!({"content": [{"type": "text", "text": "beta"}], "isError": false}),
        ..Default::default()
    })
    .await;
    let alpha_adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&alpha_url, BTreeMap::new(), 5.0, false).unwrap());
    let beta_adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&beta_url, BTreeMap::new(), 5.0, false).unwrap());
    let alpha = Project::with_backends("alpha", vec![("code".into(), alpha_adapter)]);
    let beta = Project::with_backends("beta", vec![("code".into(), beta_adapter)]);
    alpha.prewarm().await;
    beta.prewarm().await;
    let app = router([alpha, beta]);
    let alpha_session = initialize(&app, "alpha").await;
    let beta_session = initialize(&app, "beta").await;

    let (_, alpha_body) = call(
        &app,
        "/mcp/alpha",
        Some(&alpha_session),
        json!({"jsonrpc": "2.0", "id": 30, "method": "tools/list"}),
    )
    .await;
    let alpha_names: Vec<String> = sse_data(&alpha_body)["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap().to_string())
        .collect();
    assert!(alpha_names.contains(&"alpha_search".to_string()));
    assert!(!alpha_names.contains(&"beta_search".to_string()));

    let (_, beta_body) = call(
        &app,
        "/mcp/beta",
        Some(&beta_session),
        json!({"jsonrpc": "2.0", "id": 31, "method": "tools/list"}),
    )
    .await;
    let beta_names: Vec<String> = sse_data(&beta_body)["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap().to_string())
        .collect();
    assert!(beta_names.contains(&"beta_search".to_string()));
    assert!(!beta_names.contains(&"alpha_search".to_string()));

    let (_, alpha_call) = call(
        &app,
        "/mcp/alpha",
        Some(&alpha_session),
        json!({
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {"name": "alpha_search", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(
        sse_data(&alpha_call)["result"]["content"][0]["text"],
        "alpha"
    );
    let (_, beta_call) = call(
        &app,
        "/mcp/beta",
        Some(&beta_session),
        json!({
            "jsonrpc": "2.0",
            "id": 33,
            "method": "tools/call",
            "params": {"name": "beta_search", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(sse_data(&beta_call)["result"]["content"][0]["text"], "beta");

    assert!(alpha_mock
        .records
        .lock()
        .unwrap()
        .iter()
        .filter(|record| record.method == "tools/call")
        .all(|record| record.tool.as_deref() == Some("alpha_search")));
    assert!(beta_mock
        .records
        .lock()
        .unwrap()
        .iter()
        .filter(|record| record.method == "tools/call")
        .all(|record| record.tool.as_deref() == Some("beta_search")));
}

#[tokio::test]
async fn pending_cancellation_before_call_registration_is_honored() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 91}
        }),
    )
    .await;
    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 91,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(sse_data(&body)["error"]["code"], -32800);
}

#[tokio::test]
async fn request_id_reuse_after_in_flight_cancellation_is_clean() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        call_delay_ms: 150,
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let call_app = app.clone();
    let call_session = session.clone();
    let pending = tokio::spawn(async move {
        call(
            &call_app,
            "/mcp/alpha",
            Some(&call_session),
            json!({
                "jsonrpc": "2.0",
                "id": 92,
                "method": "tools/call",
                "params": {"name": "docs_recall", "arguments": {}}
            }),
        )
        .await
    });
    tokio::time::sleep(std::time::Duration::from_millis(30)).await;
    call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 92}
        }),
    )
    .await;
    let (_, body) = pending.await.expect("cancelled call");
    assert_eq!(sse_data(&body)["error"]["code"], -32800);

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 92,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(sse_data(&body)["result"]["isError"], false);
}

fn json_body(body: &str) -> Value {
    serde_json::from_str(body).expect("JSON body")
}

#[tokio::test]
async fn workspace_routing_tool_forwards_without_workspace() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("find_symbol")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.editor.backends.editor]
kind = "http"
url = "{url}"
workspace_routing = true
"#
    ))
    .unwrap();
    let adapters = build_project_adapters("editor", registry.get("editor").unwrap()).unwrap();
    let project = Project::with_backends("editor", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "editor").await;

    let (status, body) = call(
        &app,
        "/mcp/editor",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {"name": "find_symbol", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["result"]["isError"], false);

    let records = mock.records.lock().unwrap();
    let routed = records
        .iter()
        .rev()
        .find(|record| record.method == "tools/call")
        .expect("routed call");
    assert_eq!(routed.workspace, None);
    assert_eq!(routed.arguments, Some(json!({})));
}

#[tokio::test]
async fn upstream_workspace_unbound_is_relayed() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("find_symbol")],
        call_typed_unbound: true,
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.editor.backends.editor]
kind = "http"
url = "{url}"
workspace_routing = true
"#
    ))
    .unwrap();
    let adapters = build_project_adapters("editor", registry.get("editor").unwrap()).unwrap();
    let project = Project::with_backends("editor", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "editor").await;

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/mcp/editor")
                .header("mcp-session-id", &session)
                .body(Body::from(
                    json!({
                        "jsonrpc": "2.0",
                        "id": 41,
                        "method": "tools/call",
                        "params": {"name": "find_symbol", "arguments": {}}
                    })
                    .to_string(),
                ))
                .expect("request"),
        )
        .await
        .expect("router responds");
    assert_eq!(response.status(), StatusCode::CONFLICT);
    assert_eq!(response.headers()["x-engram-error"], "workspace_unbound");
    let body = to_bytes(response.into_body(), 32 * 1024).await.unwrap();
    let payload = json_body(&String::from_utf8_lossy(&body));
    assert_eq!(payload["error"]["code"], -32002);
    assert_eq!(payload["error"]["data"]["kind"], "workspace_unbound");
    assert_eq!(payload["error"]["data"]["retryable"], false);

    let records = mock.records.lock().unwrap();
    let routed = records
        .iter()
        .rev()
        .find(|record| record.method == "tools/call")
        .expect("upstream call happened");
    assert_eq!(routed.workspace, None);
}

#[tokio::test]
async fn call_before_initial_catalog_is_a_tool_error_not_flushing() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (status, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["result"]["isError"], true);
    assert!(sse_data(&body)["result"]["content"][0]["text"]
        .as_str()
        .unwrap()
        .contains("Unknown tool"));
}

#[tokio::test]
async fn call_during_refresh_uses_the_published_catalog() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        list_pause_at: Some(2),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let generation = project
        .backends
        .as_ref()
        .expect("backends")
        .freshness("alpha", None)
        .generation;

    let app = router([project.clone()]);
    let session = initialize(&app, "alpha").await;
    let refresh = tokio::spawn(async move { project.prewarm().await });

    // Wait until the second tools/list is actually blocked inside the refresh.
    mock.list_paused
        .acquire()
        .await
        .expect("refresh entered tools/list")
        .forget();

    let (status, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 43,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["result"]["isError"], false);
    let freshness = &sse_data(&body)["result"]["_meta"]["engram_freshness"];
    assert_eq!(freshness["catalog_state"], "refreshing");
    assert_eq!(freshness["catalog_generation"], generation);

    mock.list_release.add_permits(1);
    let (list_status, list_body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({"jsonrpc": "2.0", "id": 44, "method": "tools/list"}),
    )
    .await;
    assert_eq!(list_status, StatusCode::OK);
    assert!(!sse_data(&list_body)["result"]["tools"]
        .as_array()
        .unwrap()
        .is_empty());
    refresh.await.expect("refresh task");
}

#[tokio::test]
async fn optional_policy_serves_headerless_calls_and_cancellation() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router_with_options([project], SessionPolicy::Optional, OriginPolicy::default());

    // A headerless cancellation is a no-op: fencing only applies with a session.
    let (status, body) = call(
        &app,
        "/mcp/alpha",
        None,
        json!({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 77}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert!(body.is_empty());

    let (status, body) = call(
        &app,
        "/mcp/alpha",
        None,
        json!({
            "jsonrpc": "2.0",
            "id": 77,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["result"]["isError"], false);
}

#[tokio::test]
async fn write_transport_failure_is_typed_outcome_unknown() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall"), tool("retain")],
        call_result: json!({"content": [], "isError": false}),
        call_no_response: true,
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/mcp/alpha")
                .header("mcp-session-id", &session)
                .body(Body::from(
                    json!({
                        "jsonrpc": "2.0",
                        "id": 42,
                        "method": "tools/call",
                        "params": {"name": "docs_retain", "arguments": {"content": "x"}}
                    })
                    .to_string(),
                ))
                .expect("request"),
        )
        .await
        .expect("router responds");
    assert_eq!(response.status(), StatusCode::CONFLICT);
    assert_eq!(response.headers()["x-engram-error"], "outcome_unknown");
    let body = to_bytes(response.into_body(), 32 * 1024).await.unwrap();
    let payload = json_body(&String::from_utf8_lossy(&body));
    assert_eq!(payload["error"]["data"]["kind"], "outcome_unknown");
    assert_eq!(payload["error"]["data"]["retryable"], false);
}

#[tokio::test]
async fn freshness_envelope_is_attached_to_catalog_and_results() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({"jsonrpc": "2.0", "id": 43, "method": "tools/list"}),
    )
    .await;
    let list = sse_data(&body);
    assert_eq!(
        list["result"]["_meta"]["engram_freshness"]["schema_version"],
        "engram-freshness.v1"
    );
    assert_eq!(
        list["result"]["_meta"]["engram_freshness"]["catalog_state"],
        "ready"
    );
    assert_eq!(
        list["result"]["_meta"]["engram_freshness"]["project"],
        "alpha"
    );

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 44,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    let call_payload = sse_data(&body);
    assert_eq!(
        call_payload["result"]["_meta"]["engram_freshness"]["catalog_state"],
        "ready"
    );
    assert!(
        call_payload["result"]["_meta"]["engram_freshness"]["catalog_generation"]
            .as_u64()
            .unwrap()
            > 0
    );
}

#[tokio::test]
async fn read_transport_failure_remains_a_tool_error() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        call_fail_first: 5,
        call_fail_status: Some(503),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (status, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 45,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["result"]["isError"], true);
}

#[tokio::test]
async fn definitive_write_rejection_is_not_outcome_unknown() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("retain")],
        call_jsonrpc_error: true,
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (status, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 46,
            "method": "tools/call",
            "params": {"name": "docs_retain", "arguments": {"content": "x"}}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["result"]["isError"], true);
}

#[tokio::test]
async fn upstream_typed_busy_is_relayed_to_the_client() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_typed_busy: true,
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0, false).unwrap());
    let project = Project::with_backends("alpha", vec![("docs".into(), adapter)]);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/mcp/alpha")
                .header("mcp-session-id", &session)
                .body(Body::from(
                    json!({
                        "jsonrpc": "2.0",
                        "id": 47,
                        "method": "tools/call",
                        "params": {"name": "docs_recall", "arguments": {}}
                    })
                    .to_string(),
                ))
                .expect("request"),
        )
        .await
        .expect("router responds");
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(response.headers()["x-engram-error"], "workspace_flushing");
    assert_eq!(response.headers()["retry-after"], "2");
    let body = to_bytes(response.into_body(), 32 * 1024).await.unwrap();
    let payload = json_body(&String::from_utf8_lossy(&body));
    assert_eq!(payload["error"]["data"]["kind"], "workspace_flushing");
    assert_eq!(payload["error"]["data"]["workspace"], "/work");
    assert_eq!(payload["error"]["data"]["epoch"], 4);
    assert_eq!(mock.call_attempts.load(Ordering::SeqCst), 1);
}

fn fixture(name: &str) -> Value {
    match name {
        "recall_bounded_input.json" => {
            serde_json::from_str(include_str!("fixtures/parity/recall_bounded_input.json")).unwrap()
        }
        "recall_bounded_expected.json" => {
            serde_json::from_str(include_str!("fixtures/parity/recall_bounded_expected.json"))
                .unwrap()
        }
        "pattern_truncate_input.json" => {
            serde_json::from_str(include_str!("fixtures/parity/pattern_truncate_input.json"))
                .unwrap()
        }
        "pattern_truncate_expected.json" => serde_json::from_str(include_str!(
            "fixtures/parity/pattern_truncate_expected.json"
        ))
        .unwrap(),
        other => panic!("unknown fixture {other}"),
    }
}

#[tokio::test]
async fn recall_response_is_normalized_to_python_golden() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: fixture("recall_bounded_input.json"),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.alpha.backends.docs]
kind = "http"
url = "{url}"
"#
    ))
    .unwrap();
    let adapters = build_project_adapters("alpha", registry.get("alpha").unwrap()).unwrap();
    let project = Project::with_backends("alpha", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 70,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {"query": "x"}}
        }),
    )
    .await;
    let result = &sse_data(&body)["result"];
    let expected = fixture("recall_bounded_expected.json");

    assert_eq!(result["structuredContent"], expected["structuredContent"]);
    assert_eq!(result["content"], expected["content"]);
    assert_eq!(result["isError"], false);
    assert!(result["_meta"]["engram_freshness"].is_object());
}

#[tokio::test]
async fn null_arguments_are_forwarded_as_an_empty_object() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.alpha.backends.docs]
kind = "http"
url = "{url}"
"#
    ))
    .unwrap();
    let adapters = build_project_adapters("alpha", registry.get("alpha").unwrap()).unwrap();
    let project = Project::with_backends("alpha", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, _) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 76,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": null}
        }),
    )
    .await;

    let records = mock.records.lock().unwrap();
    let routed = records
        .iter()
        .rev()
        .find(|record| record.method == "tools/call")
        .expect("routed call");
    assert_eq!(routed.arguments, Some(json!({})));
}

#[tokio::test]
async fn legacy_prefixed_recall_is_normalized() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("docs_recall")],
        call_result: fixture("recall_bounded_input.json"),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.legacy]
endpoint = "{url}"
"#
    ))
    .unwrap();
    let adapters = build_project_adapters("legacy", registry.get("legacy").unwrap()).unwrap();
    let project = Project::with_backends("legacy", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "legacy").await;

    let (_, body) = call(
        &app,
        "/mcp/legacy",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 71,
            "method": "tools/call",
            "params": {"name": "docs_recall", "arguments": {}}
        }),
    )
    .await;
    let result = &sse_data(&body)["result"];
    let expected = fixture("recall_bounded_expected.json");
    assert_eq!(result["structuredContent"], expected["structuredContent"]);
    assert_eq!(result["content"], expected["content"]);
}

#[tokio::test]
async fn non_recall_results_payload_passes_through_unshaped() {
    let raw = json!({
        "content": [{"type": "text", "text": "raw write result"}],
        "isError": false,
        "structuredContent": {"results": [{"id": "not-a-recall", "text": "keep"}]}
    });
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("retain")],
        call_result: raw.clone(),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.alpha.backends.docs]
kind = "http"
url = "{url}"
"#
    ))
    .unwrap();
    let adapters = build_project_adapters("alpha", registry.get("alpha").unwrap()).unwrap();
    let project = Project::with_backends("alpha", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 74,
            "method": "tools/call",
            "params": {"name": "docs_retain", "arguments": {"content": "x"}}
        }),
    )
    .await;
    let result = &sse_data(&body)["result"];
    assert_eq!(result["content"], raw["content"]);
    assert_eq!(result["structuredContent"], raw["structuredContent"]);
    assert!(result["_meta"]["engram_freshness"].is_object());
}

#[tokio::test]
async fn search_for_pattern_exposes_and_applies_max_answer_chars() {
    let (url, mock) = spawn_mock(MockConfig {
        tools: vec![tool("zvec_grep_search"), tool("zvec_grep_rg")],
        call_result: fixture("pattern_truncate_input.json"),
        ..Default::default()
    })
    .await;
    let registry = Registry::parse_str(&format!(
        r#"
[instances.alpha.backends.zg]
kind = "http"
url = "{url}"
"#
    ))
    .unwrap();
    let adapters = build_project_adapters("alpha", registry.get("alpha").unwrap()).unwrap();
    let project = Project::with_backends("alpha", adapters);
    project.prewarm().await;
    let app = router([project]);
    let session = initialize(&app, "alpha").await;

    let (_, list_body) = call(
        &app,
        "/mcp/alpha",
        Some(&session),
        json!({"jsonrpc": "2.0", "id": 72, "method": "tools/list"}),
    )
    .await;
    let list_payload = sse_data(&list_body);
    let tools = list_payload["result"]["tools"].as_array().unwrap();
    let alias = tools
        .iter()
        .find(|tool| tool["name"] == "search_for_pattern")
        .expect("alias injected");
    assert_eq!(
        alias["inputSchema"]["properties"]["max_answer_chars"]["default"],
        -1
    );

    let (_, body) = call(
        &app,
        "/mcp/alpha?workspace=%2Fwork",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 73,
            "method": "tools/call",
            "params": {
                "name": "search_for_pattern",
                "arguments": {"substring_pattern": "0123456789", "max_answer_chars": 20}
            }
        }),
    )
    .await;
    let result = &sse_data(&body)["result"];
    let expected = fixture("pattern_truncate_expected.json");
    assert_eq!(result["content"], expected["content"]);
    assert_eq!(result["isError"], false);

    {
        let records = mock.records.lock().unwrap();
        let routed_call = records
            .iter()
            .rev()
            .find(|record| record.method == "tools/call")
            .expect("routed call");
        let command = routed_call.arguments.as_ref().unwrap()["command"]
            .as_str()
            .unwrap();
        assert!(command.contains("0123456789"), "{command}");
        assert!(command.contains("-e"), "{command}");
    }

    // Without max_answer_chars the backend text is returned intact.
    let (_, untruncated) = call(
        &app,
        "/mcp/alpha?workspace=%2Fwork",
        Some(&session),
        json!({
            "jsonrpc": "2.0",
            "id": 75,
            "method": "tools/call",
            "params": {
                "name": "search_for_pattern",
                "arguments": {"substring_pattern": "0123456789"}
            }
        }),
    )
    .await;
    let untruncated = &sse_data(&untruncated)["result"];
    let input = fixture("pattern_truncate_input.json");
    assert_eq!(untruncated["content"], input["content"]);
}
