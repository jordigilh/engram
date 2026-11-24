//! Integration tests for the stdio MCP adapter and its gateway route.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use engram_mcp::{
    build_project_adapters, router, BackendAdapter, Project, Registry, StdioSubprocessAdapter,
};
use serde_json::{json, Value};
use tower::ServiceExt;

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        static NEXT: AtomicU32 = AtomicU32::new(0);
        let path = std::env::temp_dir().join(format!(
            "engram-stdio-test-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&path).expect("temp dir");
        Self(path)
    }

    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }

    fn read(&self, name: &str) -> String {
        std::fs::read_to_string(self.path(name)).unwrap_or_default()
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn fixture() -> String {
    concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/stdio_mock_mcp.py"
    )
    .to_string()
}

fn adapter(mode: &str) -> (StdioSubprocessAdapter, TempDir) {
    let dir = TempDir::new();
    let mut env = BTreeMap::new();
    env.insert("STDIO_MOCK_MODE".into(), mode.into());
    env.insert(
        "STDIO_MOCK_LOG".into(),
        dir.path("log").to_string_lossy().into_owned(),
    );
    env.insert(
        "STDIO_MOCK_STATE".into(),
        dir.path("state").to_string_lossy().into_owned(),
    );
    env.insert(
        "STDIO_MOCK_GATE".into(),
        dir.path("release").to_string_lossy().into_owned(),
    );
    (
        StdioSubprocessAdapter::new("python3".into(), vec![fixture()], env),
        dir,
    )
}

#[tokio::test]
async fn lists_tools_and_normalizes_descriptions() {
    let (adapter, _dir) = adapter("normal");
    let tools = adapter.list_tools().await.expect("tools");
    assert_eq!(tools.len(), 2);
    assert_eq!(tools[0]["name"], "alpha");
    assert_eq!(tools[0]["description"], "Alpha tool");
    assert_eq!(tools[1]["name"], "beta");
    assert_eq!(tools[1]["description"], "");
    assert_eq!(tools[0]["inputSchema"]["type"], "object");
}

#[tokio::test]
async fn calls_tools_and_strips_nulls_and_extra_fields() {
    let (adapter, _dir) = adapter("normal");
    let result = adapter
        .call_tool("alpha", json!({"query": "x"}))
        .await
        .expect("call");
    assert_eq!(result["isError"], false);
    assert_eq!(result["content"][0]["text"], "echo");
    assert!(result["content"][0].get("annotations").is_none());
    assert!(result.get("structuredContent").is_none());
}

#[tokio::test]
async fn follows_tool_pagination() {
    let (adapter, _dir) = adapter("paginate");
    let tools = adapter.list_tools().await.expect("tools");
    let names: Vec<&str> = tools
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["alpha", "beta"]);
}

#[tokio::test]
async fn negotiates_an_old_protocol_version() {
    let (adapter, _dir) = adapter("old-protocol");
    let tools = adapter.list_tools().await.expect("tools");
    assert_eq!(tools.len(), 2);
    adapter
        .call_tool("alpha", json!({}))
        .await
        .expect("call after old-protocol initialize");
}

#[tokio::test]
async fn tolerates_a_server_to_client_request_mid_call() {
    let (adapter, _dir) = adapter("request-then-respond");
    let result = tokio::time::timeout(
        Duration::from_secs(5),
        adapter.call_tool("alpha", json!({})),
    )
    .await
    .expect("call must not hang")
    .expect("call");
    assert_eq!(result["content"][0]["text"], "echo");
}

#[tokio::test]
async fn a_cancelled_call_does_not_poison_the_session() {
    let (adapter, dir) = adapter("slow");
    let adapter = Arc::new(adapter);
    let first = tokio::spawn({
        let adapter = Arc::clone(&adapter);
        async move { adapter.call_tool("alpha", json!({})).await }
    });
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !dir.read("log").contains("call ") {
        assert!(
            tokio::time::Instant::now() < deadline,
            "first call never reached the child"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    first.abort();
    let joined = first.await;
    assert!(joined.expect_err("aborted task").is_cancelled());

    let result = tokio::time::timeout(
        Duration::from_secs(5),
        adapter.call_tool("alpha", json!({})),
    )
    .await
    .expect("second call must not hang")
    .expect("second call");
    assert_eq!(result["content"][0]["text"], "echo");

    let log = dir.read("log");
    let lines: Vec<&str> = log.lines().collect();
    let starts = lines
        .iter()
        .filter(|line| line.starts_with("start "))
        .count();
    assert_eq!(
        starts, 2,
        "cancelled call must retire the child; log {log:?}"
    );
    let first_exit = lines.iter().position(|line| line.starts_with("exit "));
    let second_start = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.starts_with("start "))
        .nth(1)
        .map(|(index, _)| index);
    assert!(
        first_exit.is_some() && first_exit < second_start,
        "next call must wait for the retired child to exit; log {log:?}"
    );
}

#[tokio::test]
async fn cancellation_during_initialization_keeps_cleanup_waitable() {
    let (adapter, dir) = adapter("slow-init");
    let adapter = Arc::new(adapter);
    let first = tokio::spawn({
        let adapter = Arc::clone(&adapter);
        async move { adapter.call_tool("alpha", json!({})).await }
    });
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !dir.read("log").contains("start ") {
        assert!(
            tokio::time::Instant::now() < deadline,
            "initialization never started"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    first.abort();
    assert!(first.await.expect_err("aborted").is_cancelled());

    let result = tokio::time::timeout(
        Duration::from_secs(8),
        adapter.call_tool("alpha", json!({})),
    )
    .await
    .expect("second call must not hang")
    .expect("second call");
    assert_eq!(result["content"][0]["text"], "echo");

    let log = dir.read("log");
    let lines: Vec<&str> = log.lines().collect();
    let starts = lines
        .iter()
        .filter(|line| line.starts_with("start "))
        .count();
    assert_eq!(starts, 2, "one failed init + one success; log {log:?}");
    let first_exit = lines.iter().position(|line| line.starts_with("exit "));
    let second_start = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.starts_with("start "))
        .nth(1)
        .map(|(index, _)| index);
    assert!(
        first_exit.is_some() && first_exit < second_start,
        "replacement must wait for failed init child; log {log:?}"
    );
}

#[tokio::test]
async fn cancellation_during_retirement_keeps_the_cleanup_waitable() {
    let (adapter, dir) = adapter("gated-exit");
    let adapter = Arc::new(adapter);

    let first = tokio::spawn({
        let adapter = Arc::clone(&adapter);
        async move { adapter.call_tool("alpha", json!({})).await }
    });
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !dir.read("log").contains("call ") {
        assert!(
            tokio::time::Instant::now() < deadline,
            "first call never reached the child"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    first.abort();
    assert!(first.await.expect_err("aborted").is_cancelled());

    let handles_before = adapter.retirement_handle_waits();
    let second = tokio::spawn({
        let adapter = Arc::clone(&adapter);
        async move { adapter.call_tool("alpha", json!({})).await }
    });
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while adapter.retirement_handle_waits() <= handles_before {
        assert!(
            tokio::time::Instant::now() < deadline,
            "second caller never took the cleanup handle"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    second.abort();
    assert!(second.await.expect_err("aborted").is_cancelled());

    // Release the gated child so the third call can finish retirement.
    std::fs::write(dir.path("release"), b"1").expect("release gate");

    let result = tokio::time::timeout(
        Duration::from_secs(8),
        adapter.call_tool("alpha", json!({})),
    )
    .await
    .expect("third call must not hang")
    .expect("third call");
    assert_eq!(result["content"][0]["text"], "echo");

    let log = dir.read("log");
    let lines: Vec<&str> = log.lines().collect();
    let starts = lines
        .iter()
        .filter(|line| line.starts_with("start "))
        .count();
    assert_eq!(starts, 2, "only one replacement child; log {log:?}");
    let first_exit = lines.iter().position(|line| line.starts_with("exit "));
    let second_start = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.starts_with("start "))
        .nth(1)
        .map(|(index, _)| index);
    assert!(
        first_exit.is_some() && first_exit < second_start,
        "replacement must wait for retirement; log {log:?}"
    );
}

#[tokio::test]
async fn write_calls_are_not_replayed_after_an_uncertain_failure() {
    let (adapter, dir) = adapter("die-once");
    let error = adapter
        .call_tool("retain", json!({"content": "x"}))
        .await
        .expect_err("write must not be reported as successful");
    assert!(error.uncertain, "{error:?}");
    let log = dir.read("log");
    let starts = log
        .lines()
        .filter(|line| line.starts_with("start "))
        .count();
    let calls = log.lines().filter(|line| line.starts_with("call ")).count();
    assert_eq!(
        starts, 2,
        "write must restart for later calls; log was {log:?}"
    );
    assert_eq!(calls, 1, "write must not be replayed; log was {log:?}");

    adapter
        .call_tool("retain", json!({"content": "next"}))
        .await
        .expect("the restarted child serves the next write");
    let log = dir.read("log");
    let calls = log.lines().filter(|line| line.starts_with("call ")).count();
    assert_eq!(calls, 2, "second write is a new call; log was {log:?}");
}

#[tokio::test]
async fn definitive_tools_list_rejection_does_not_restart() {
    let (adapter, dir) = adapter("list-error");
    let error = adapter.list_tools().await.expect_err("rejected tools/list");
    assert!(!error.transient, "{error:?}");
    let log = dir.read("log");
    let starts = log
        .lines()
        .filter(|line| line.starts_with("start "))
        .count();
    assert_eq!(
        starts, 1,
        "tools/list rejection must not restart; log {log:?}"
    );
}

#[tokio::test]
async fn definitive_jsonrpc_rejection_is_not_outcome_unknown() {
    let (adapter, dir) = adapter("jsonrpc-error");
    let write_error = adapter
        .call_tool("retain", json!({"content": "x"}))
        .await
        .expect_err("rejected write");
    assert!(!write_error.uncertain, "{write_error:?}");
    assert!(!write_error.transient, "{write_error:?}");

    let read_error = adapter
        .call_tool("recall", json!({}))
        .await
        .expect_err("rejected read");
    assert!(!read_error.transient, "{read_error:?}");

    let log = dir.read("log");
    let starts = log
        .lines()
        .filter(|line| line.starts_with("start "))
        .count();
    assert_eq!(
        starts, 1,
        "definitive rejections must not restart; log {log:?}"
    );
}

#[tokio::test]
async fn restarts_once_after_the_child_dies() {
    let (adapter, dir) = adapter("die-once");
    let result = adapter
        .call_tool("recall", json!({}))
        .await
        .expect("retry after restart");
    assert_eq!(result["content"][0]["text"], "echo");
    let log = dir.read("log");
    let starts = log
        .lines()
        .filter(|line| line.starts_with("start "))
        .count();
    assert_eq!(starts, 2, "log was {log:?}");
}

#[tokio::test]
async fn malformed_output_fails_after_one_retry() {
    let (adapter, _dir) = adapter("malformed");
    let error = tokio::time::timeout(
        Duration::from_secs(10),
        adapter.call_tool("recall", json!({})),
    )
    .await
    .expect("must not hang")
    .expect_err("malformed output must fail");
    assert!(
        error.message.contains("stdio tools/call failed"),
        "{error:?}"
    );
}

#[tokio::test]
async fn gateway_routes_a_stdio_backend() {
    let dir = TempDir::new();
    let registry = Registry::parse_str(&format!(
        r#"
[instances.alpha.backends.stdio]
kind = "stdio"
workspace_routing = false
command = "python3"
args = ["{}"]
shared_key = "family"

[instances.alpha.backends.stdio.env]
STDIO_MOCK_MODE = "normal"
STDIO_MOCK_LOG = "{}"
"#,
        fixture().replace('\\', "\\\\"),
        dir.path("log").display()
    ))
    .unwrap();
    let adapters = build_project_adapters("alpha", registry.get("alpha").unwrap()).unwrap();
    let project = Project::with_backends("alpha", adapters);
    project.prewarm().await;
    let app = router([project]);

    let (status, session) = initialize(&app).await;
    assert_eq!(status, StatusCode::OK);

    let (status, tools) = post(
        &app,
        &session,
        json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let names: Vec<&str> = tools["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["alpha", "beta"]);

    let (status, result) = post(
        &app,
        &session,
        json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "alpha", "arguments": {}}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(result["result"]["isError"], false);
}

async fn initialize(app: &axum::Router) -> (StatusCode, String) {
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/mcp/alpha")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                        .to_string(),
                ))
                .expect("request"),
        )
        .await
        .expect("router");
    let status = response.status();
    let session = response
        .headers()
        .get("mcp-session-id")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_string();
    (status, session)
}

async fn post(app: &axum::Router, session: &str, body: Value) -> (StatusCode, Value) {
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/mcp/alpha")
                .header("content-type", "application/json")
                .header("mcp-session-id", session)
                .body(Body::from(body.to_string()))
                .expect("request"),
        )
        .await
        .expect("router");
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 64 * 1024)
        .await
        .expect("body");
    let text = String::from_utf8_lossy(&bytes);
    let value = text
        .split_once("data: ")
        .map(|(_, payload)| payload.trim())
        .and_then(|payload| serde_json::from_str(payload).ok())
        .unwrap_or(Value::Null);
    (status, value)
}
