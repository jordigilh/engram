//! End-to-end HTTP tests for the #211 slice-1 lifecycle contract.

use axum::body::{to_bytes, Body};
use axum::http::{header, HeaderMap, Request, StatusCode};
use axum::Router;
use engram_mcp::server::tool_definition;
use engram_mcp::{router, router_with_options, router_with_policy, Project, SessionPolicy};
use serde_json::{json, Value};
use tower::ServiceExt;

fn app() -> Router {
    router([
        Project::with_tools(
            "alpha",
            vec![tool_definition("alpha_search", "Alpha search")],
        ),
        Project::with_tools("beta", vec![tool_definition("beta_search", "Beta search")]),
    ])
}

async fn call(
    app: &Router,
    method: &str,
    uri: &str,
    session: Option<&str>,
    body: Option<Value>,
) -> (StatusCode, HeaderMap, String) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(session) = session {
        builder = builder.header("mcp-session-id", session);
    }
    let body = match body {
        Some(value) => Body::from(value.to_string()),
        None => Body::empty(),
    };
    let response = app
        .clone()
        .oneshot(builder.body(body).expect("valid request"))
        .await
        .expect("router responds");
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = to_bytes(response.into_body(), 32 * 1024)
        .await
        .expect("bounded body");
    (
        status,
        headers,
        String::from_utf8(bytes.to_vec()).expect("utf-8 body"),
    )
}

fn sse_data(body: &str) -> Value {
    for line in body.lines() {
        if let Some(payload) = line.strip_prefix("data: ") {
            return serde_json::from_str(payload).expect("SSE payload is JSON");
        }
    }
    panic!("no SSE data frame in {body:?}");
}

async fn initialize(app: &Router, project: &str) -> String {
    let (status, headers, body) = call(
        app,
        "POST",
        &format!("/mcp/{project}"),
        None,
        Some(json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["result"]["protocolVersion"], "2024-11-05");
    headers
        .get("mcp-session-id")
        .expect("initialize returns a session id")
        .to_str()
        .expect("session id is ASCII")
        .to_string()
}

#[tokio::test]
async fn initialize_returns_contract_result_and_session_header() {
    let app = app();
    let (status, headers, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        None,
        Some(json!({"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}})),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok()),
        Some("text/event-stream")
    );
    assert!(headers.contains_key("mcp-session-id"));

    let payload = sse_data(&body);
    assert_eq!(payload["id"], 9);
    assert_eq!(payload["result"]["capabilities"]["tools"], json!({}));
    assert_eq!(payload["result"]["serverInfo"]["name"], "engram-gateway");
}

#[tokio::test]
async fn initialized_notification_is_accepted_without_a_body() {
    let app = app();
    let session = initialize(&app, "alpha").await;

    let (status, _, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({"jsonrpc": "2.0", "method": "notifications/initialized"})),
    )
    .await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert!(body.is_empty());
}

#[tokio::test]
async fn reinitialize_reuses_a_valid_session() {
    let app = app();
    let session = initialize(&app, "alpha").await;

    let (status, headers, _) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        headers
            .get("mcp-session-id")
            .and_then(|value| value.to_str().ok()),
        Some(session.as_str())
    );
}

#[tokio::test]
async fn ping_succeeds_and_unknown_methods_are_method_not_found() {
    let app = app();
    let session = initialize(&app, "alpha").await;

    let (status, _, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({"jsonrpc": "2.0", "id": 20, "method": "ping"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["result"], json!({}));

    let (_, _, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({"jsonrpc": "2.0", "id": 21, "method": "no/such/method"})),
    )
    .await;
    assert_eq!(sse_data(&body)["error"]["code"], -32601);
}

#[tokio::test]
async fn host_header_must_be_loopback() {
    let app = app();
    let init = json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}});

    for host in ["evil.example", "localhost.evil.example"] {
        let rejected = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp/alpha")
                    .header("host", host)
                    .body(Body::from(init.to_string()))
                    .expect("valid request"),
            )
            .await
            .expect("router responds");
        assert_eq!(
            rejected.status(),
            StatusCode::FORBIDDEN,
            "host {host:?} must be rejected"
        );
    }

    for host in [
        "localhost:8898",
        "LOCALHOST:8898",
        "localhost.:8898",
        "127.0.0.1:8898",
        "[::1]:8898",
    ] {
        let allowed = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp/alpha")
                    .header("host", host)
                    .body(Body::from(init.to_string()))
                    .expect("valid request"),
            )
            .await
            .expect("router responds");
        assert_eq!(
            allowed.status(),
            StatusCode::OK,
            "loopback host {host:?} must be allowed"
        );
    }
}

#[tokio::test]
async fn tools_list_is_scoped_to_the_project() {
    let app = app();
    let alpha = initialize(&app, "alpha").await;
    let beta = initialize(&app, "beta").await;

    let (status, _, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&alpha),
        Some(json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let tools = sse_data(&body)["result"]["tools"].clone();
    assert_eq!(tools.as_array().expect("tools array").len(), 1);
    assert_eq!(tools[0]["name"], "alpha_search");

    let (_, _, body) = call(
        &app,
        "POST",
        "/mcp/beta",
        Some(&beta),
        Some(json!({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})),
    )
    .await;
    let tools = sse_data(&body)["result"]["tools"].clone();
    assert_eq!(tools[0]["name"], "beta_search");
}

#[tokio::test]
async fn a_session_from_another_project_is_rejected() {
    let app = app();
    let alpha = initialize(&app, "alpha").await;

    let (status, _, body) = call(
        &app,
        "POST",
        "/mcp/beta",
        Some(&alpha),
        Some(json!({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})),
    )
    .await;

    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(body.is_empty());
}

#[tokio::test]
async fn delete_terminates_the_session() {
    let app = app();
    let session = initialize(&app, "alpha").await;

    let (status, _, _) = call(&app, "DELETE", "/mcp/alpha", Some(&session), None).await;
    assert_eq!(status, StatusCode::OK);

    let (status, _, _) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn delete_without_or_with_unknown_session_is_rejected() {
    let app = app();
    let (status, _, _) = call(&app, "DELETE", "/mcp/alpha", None, None).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, _, _) = call(&app, "DELETE", "/mcp/alpha", Some("missing"), None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn cancellation_prevents_the_cancelled_request() {
    let app = app();
    let session = initialize(&app, "alpha").await;

    let (status, _, _) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 42, "reason": "client aborted"}
        })),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);

    let (status, _, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "alpha_search", "arguments": {}}
        })),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(sse_data(&body)["error"]["code"], -32800);
}

#[tokio::test]
async fn cancellation_is_scoped_to_its_session() {
    let app = app();
    let alpha = initialize(&app, "alpha").await;
    let beta = initialize(&app, "beta").await;

    call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&alpha),
        Some(json!({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 7}
        })),
    )
    .await;

    let (_, _, body) = call(
        &app,
        "POST",
        "/mcp/beta",
        Some(&beta),
        Some(json!({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "beta_search", "arguments": {}}
        })),
    )
    .await;

    assert_eq!(sse_data(&body)["error"]["code"], -32601);
}

#[tokio::test]
async fn transport_errors_are_explicit() {
    let app = app();

    let (status, _, _) = call(&app, "GET", "/mcp/alpha", None, None).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);

    let (status, _, _) = call(
        &app,
        "POST",
        "/mcp/alpha",
        None,
        Some(json!({"oops": true})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, _, _) = call(
        &app,
        "POST",
        "/mcp/missing",
        None,
        Some(json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);

    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/mcp/alpha")
                .body(Body::from("not json"))
                .expect("valid request"),
        )
        .await
        .expect("router responds");
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn stateless_requests_are_rejected_by_default() {
    let app = app();

    let (status, _, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        None,
        Some(json!({"jsonrpc": "2.0", "id": 11, "method": "tools/list"})),
    )
    .await;

    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body.is_empty());
}

#[tokio::test]
async fn stateless_requests_can_be_enabled_for_python_parity() {
    let app = router_with_policy(
        [Project::with_tools(
            "alpha",
            vec![tool_definition("alpha_search", "Alpha search")],
        )],
        SessionPolicy::Optional,
    );

    let (status, _, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        None,
        Some(json!({"jsonrpc": "2.0", "id": 11, "method": "tools/list"})),
    )
    .await;

    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        sse_data(&body)["result"]["tools"][0]["name"],
        "alpha_search"
    );
}

#[tokio::test]
async fn delete_cannot_be_bypassed_by_dropping_the_session_header() {
    let app = app();
    let session = initialize(&app, "alpha").await;
    let (status, _, _) = call(&app, "DELETE", "/mcp/alpha", Some(&session), None).await;
    assert_eq!(status, StatusCode::OK);

    let (status, _, _) = call(
        &app,
        "POST",
        "/mcp/alpha",
        None,
        Some(json!({"jsonrpc": "2.0", "id": 12, "method": "tools/list"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn cancellation_is_one_shot() {
    let app = app();
    let session = initialize(&app, "alpha").await;

    call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 99}
        })),
    )
    .await;

    let (_, _, first) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "alpha_search", "arguments": {}}
        })),
    )
    .await;
    assert_eq!(sse_data(&first)["error"]["code"], -32800);

    let (_, _, second) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "alpha_search", "arguments": {}}
        })),
    )
    .await;
    assert_eq!(sse_data(&second)["error"]["code"], -32601);
}

#[tokio::test]
async fn explicit_null_id_is_treated_as_a_notification() {
    let app = app();

    let (status, _, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        None,
        Some(json!({"jsonrpc": "2.0", "id": null, "method": "initialize"})),
    )
    .await;

    assert_eq!(status, StatusCode::ACCEPTED);
    assert!(body.is_empty());
}

#[tokio::test]
async fn non_utf8_session_header_is_rejected() {
    let app = app();
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/mcp/alpha")
                .header(
                    "mcp-session-id",
                    axum::http::HeaderValue::from_bytes(b"\xff").expect("opaque header"),
                )
                .body(Body::from(
                    json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).to_string(),
                ))
                .expect("valid request"),
        )
        .await
        .expect("router responds");

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn origin_validation_rejects_external_and_allows_loopback() {
    let app = app();
    let init = json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}});

    for origin in [
        "https://evil.example",
        "http://localhost.evil.example",
        "http://user@localhost.evil.example",
        "null",
    ] {
        let rejected = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp/alpha")
                    .header("origin", origin)
                    .body(Body::from(init.to_string()))
                    .expect("valid request"),
            )
            .await
            .expect("router responds");
        assert_eq!(
            rejected.status(),
            StatusCode::FORBIDDEN,
            "origin {origin:?} must be rejected"
        );
    }

    for origin in [
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://[::1]:3000",
    ] {
        let loopback = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp/alpha")
                    .header("origin", origin)
                    .body(Body::from(init.to_string()))
                    .expect("valid request"),
            )
            .await
            .expect("router responds");
        assert_eq!(
            loopback.status(),
            StatusCode::OK,
            "loopback origin {origin:?} must be allowed"
        );
    }
}

#[tokio::test]
async fn configured_origin_is_allowed() {
    let app = router_with_options(
        [Project::new("alpha")],
        SessionPolicy::Required,
        engram_mcp::OriginPolicy {
            allow_no_origin: true,
            allowed_origins: vec!["https://app.example".into()],
        },
    );

    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/mcp/alpha")
                .header("origin", "https://app.example")
                .body(Body::from(
                    json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).to_string(),
                ))
                .expect("valid request"),
        )
        .await
        .expect("router responds");
    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn initialize_rejects_deleted_or_cross_project_sessions() {
    let app = app();
    let alpha = initialize(&app, "alpha").await;
    let (status, _, _) = call(&app, "DELETE", "/mcp/alpha", Some(&alpha), None).await;
    assert_eq!(status, StatusCode::OK);

    let (status, _, _) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&alpha),
        Some(json!({"jsonrpc": "2.0", "id": 1, "method": "initialize"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);

    let beta_session = initialize(&app, "beta").await;
    let (status, _, _) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&beta_session),
        Some(json!({"jsonrpc": "2.0", "id": 2, "method": "initialize"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn malformed_cancellation_does_not_record_a_fence() {
    let app = app();
    let session = initialize(&app, "alpha").await;

    let (status, _, _) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "notifications/cancelled",
            "params": {"requestId": 42}
        })),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, _, _) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({
            "jsonrpc": "1.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 42}
        })),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (_, _, body) = call(
        &app,
        "POST",
        "/mcp/alpha",
        Some(&session),
        Some(json!({
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "alpha_search", "arguments": {}}
        })),
    )
    .await;
    assert_eq!(sse_data(&body)["error"]["code"], -32601);
}
