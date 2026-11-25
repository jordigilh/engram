//! HTTP behavior of the Hindsight sink.

mod support;

use std::collections::BTreeMap;
use std::time::Duration;

use engram_ingest::{DeleteOutcome, HindsightSink, RetainRequest};

fn retain_request() -> RetainRequest {
    let mut metadata = BTreeMap::new();
    metadata.insert("source".to_string(), "cocoindex".to_string());
    RetainRequest {
        bank_id: "repo-docs".to_string(),
        document_id: "repo--a".to_string(),
        content: "body".to_string(),
        timestamp: Some("2026-11-24T10:15:30".to_string()),
        metadata,
        tags: vec!["root".to_string(), "repo".to_string()],
    }
}

#[test]
fn retain_posts_one_item_and_accepts_2xx() {
    let (url, log) = support::one_shot(200, r#"{"ok":true}"#);
    let sink = HindsightSink::new(url)
        .unwrap()
        .with_timeouts(Duration::from_secs(2), Duration::from_secs(2));
    sink.retain(&retain_request()).unwrap();
    let request = log.lock().unwrap()[0].clone();
    assert!(request.starts_with("POST /v1/default/banks/repo-docs/memories"));
    assert!(request.contains("\"items\""));
    assert!(request.contains("\"document_id\":\"repo--a\""));
    assert!(request.contains("\"source\":\"cocoindex\""));
}

#[test]
fn server_error_is_transient() {
    let (url, _) = support::one_shot(503, "{}");
    let sink = HindsightSink::new(url).unwrap();
    let error = sink.retain(&retain_request()).unwrap_err();
    assert!(error.is_transient(), "{error}");
}

#[test]
fn bad_request_is_permanent() {
    let (url, _) = support::one_shot(400, "{}");
    let sink = HindsightSink::new(url).unwrap();
    let error = sink.retain(&retain_request()).unwrap_err();
    assert!(!error.is_transient(), "{error}");
}

#[test]
fn delete_404_is_already_absent() {
    let (url, log) = support::one_shot(404, "{}");
    let sink = HindsightSink::new(url).unwrap();
    let outcome = sink.delete_document("repo-docs", "repo--a").unwrap();
    assert_eq!(outcome, DeleteOutcome::AlreadyAbsent);
    assert!(
        log.lock().unwrap()[0].starts_with("DELETE /v1/default/banks/repo-docs/documents/repo--a")
    );
}

#[test]
fn delete_2xx_is_deleted() {
    let (url, _) = support::one_shot(204, "");
    let sink = HindsightSink::new(url).unwrap();
    assert_eq!(
        sink.delete_document("repo-docs", "repo--a").unwrap(),
        DeleteOutcome::Deleted
    );
}
