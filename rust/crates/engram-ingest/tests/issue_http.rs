//! Real reqwest transport checks against the local one-shot HTTP server.

mod support;

use std::time::Duration;

use engram_ingest::{HttpTransport, PollError, ReqwestTransport};
use serde_json::json;

#[test]
fn reqwest_transport_get_and_post() {
    let (url, log) = support::one_shot(200, "{\"ok\":true}");
    let transport = ReqwestTransport::new(Duration::from_secs(5)).expect("transport");
    let response = transport
        .get(&format!("{url}/path"), &[("X-Test", "1".to_string())])
        .expect("get");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("content-type"), Some("application/json"));
    assert!(String::from_utf8_lossy(&response.body).contains("ok"));
    assert!(log.lock().unwrap()[0].to_lowercase().contains("x-test: 1"));

    let (url, _) = support::one_shot(201, "{}");
    let response = transport
        .post_json(&format!("{url}/submit"), &[], &json!({"a": 1}))
        .expect("post");
    assert_eq!(response.status, 201);
}

#[test]
fn reqwest_transport_surfaces_timeouts() {
    let (url, _) = support::one_shot_delayed(200, "{}", Duration::from_millis(500));
    let transport =
        ReqwestTransport::new(Duration::from_millis(50)).expect("transport with short timeout");
    let error = transport.get(&url, &[]).unwrap_err();
    assert!(matches!(error, PollError::Transport(_)));
}
