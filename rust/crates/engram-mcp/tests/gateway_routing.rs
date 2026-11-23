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
use engram_mcp::{build_project_adapters, router, Project, Registry};
use serde_json::{json, Value};
use tower::ServiceExt;

#[derive(Clone)]
struct MockState {
    tools: Vec<Value>,
    call_result: Value,
    list_fail_first: usize,
    list_always_fail: Arc<std::sync::atomic::AtomicBool>,
    call_fail_first: usize,
    call_fail_status: u16,
    call_jsonrpc_error: bool,
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
    workspace: Option<String>,
    init: Option<Value>,
}

#[derive(Default)]
struct MockConfig {
    tools: Vec<Value>,
    call_result: Value,
    list_fail_first: usize,
    list_always_fail: bool,
    call_fail_first: usize,
    call_fail_status: Option<u16>,
    call_jsonrpc_error: bool,
}

async fn spawn_mock(config: MockConfig) -> (String, MockState) {
    let state = MockState {
        tools: config.tools,
        call_result: config.call_result,
        list_fail_first: config.list_fail_first,
        list_always_fail: Arc::new(std::sync::atomic::AtomicBool::new(config.list_always_fail)),
        call_fail_first: config.call_fail_first,
        call_fail_status: config.call_fail_status.unwrap_or(503),
        call_jsonrpc_error: config.call_jsonrpc_error,
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
        workspace: query.get("workspace").cloned(),
        init: (method == "initialize").then(|| body.clone()),
    });

    match method.as_str() {
        "initialize" => {
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
            if state.list_always_fail.load(Ordering::SeqCst) || attempt <= state.list_fail_first {
                return StatusCode::SERVICE_UNAVAILABLE.into_response();
            }
            let payload =
                json!({"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": state.tools}});
            (
                [(axum::http::header::CONTENT_TYPE, "text/event-stream")],
                format!("event: message\ndata: {payload}\n\n"),
            )
                .into_response()
        }
        "tools/call" => {
            let attempt = state.call_attempts.fetch_add(1, Ordering::SeqCst) + 1;
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
            Json(json!({"jsonrpc": "2.0", "id": body.get("id"), "result": state.call_result}))
                .into_response()
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
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0).expect("adapter");

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
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0).expect("adapter");

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
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0).expect("adapter");
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
async fn tools_call_works_immediately_after_prewarm() {
    let (url, _mock) = spawn_mock(MockConfig {
        tools: vec![tool("recall")],
        call_result: json!({"content": [], "isError": false}),
        ..Default::default()
    })
    .await;
    let adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0).unwrap());
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
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0).unwrap());
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
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0).unwrap());
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
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0).expect("adapter");
    let error = adapter
        .call_tool("recall", json!({}))
        .await
        .expect_err("500 is permanent");
    assert!(error.message.contains("HTTP 500"));
    assert_eq!(mock.call_attempts.load(Ordering::SeqCst), 1);

    let (url, mock) = spawn_mock(MockConfig {
        call_jsonrpc_error: true,
        ..Default::default()
    })
    .await;
    let adapter = HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0).expect("adapter");
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
        Arc::new(HttpRelayAdapter::new(&url, BTreeMap::new(), 5.0).unwrap());
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
    let dead_adapter: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new("http://127.0.0.1:1/mcp", BTreeMap::new(), 1.0).unwrap());
    let live: Arc<dyn BackendAdapter> =
        Arc::new(HttpRelayAdapter::new(&docs_url, BTreeMap::new(), 5.0).unwrap());
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
