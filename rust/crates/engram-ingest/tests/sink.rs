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
    assert!(request.contains("\"timestamp\":\"2026-11-24T10:15:30\""));
    assert!(request.contains("\"tags\":[\"root\",\"repo\"]"));
}

#[test]
fn document_id_path_segments_are_percent_encoded() {
    let (url, log) = support::one_shot(204, "");
    let sink = HindsightSink::new(url).unwrap();
    sink.delete_document("repo-docs", "a/b?x=1").unwrap();
    let request = log.lock().unwrap()[0].clone();
    assert!(
        request.starts_with("DELETE /v1/default/banks/repo-docs/documents/a%2Fb%3Fx=1 "),
        "{request}"
    );
}

#[test]
fn redirects_are_not_followed() {
    let (url, log) = support::one_shot_redirect(302, "/login");
    let sink = HindsightSink::new(url).unwrap();
    let error = sink.retain(&retain_request()).unwrap_err();
    assert!(!error.is_transient(), "{error}");
    assert_eq!(
        log.lock().unwrap().len(),
        1,
        "redirect must not be followed"
    );
}

#[test]
fn base_url_path_prefix_is_preserved() {
    let (url, log) = support::one_shot(200, "{}");
    let sink = HindsightSink::new(format!("{url}/hindsight")).unwrap();
    sink.retain(&retain_request()).unwrap();
    let request = log.lock().unwrap()[0].clone();
    assert!(
        request.starts_with("POST /hindsight/v1/default/banks/repo-docs/memories "),
        "{request}"
    );
}

#[test]
fn base_url_credentials_are_rejected() {
    let error = HindsightSink::new("http://user:secret@127.0.0.1:1").unwrap_err();
    assert!(error.to_string().contains("credentials"), "{error}");
}

#[test]
fn timeout_is_transient() {
    let (url, _) = support::one_shot_delayed(200, "{}", Duration::from_millis(300));
    let sink = HindsightSink::new(url)
        .unwrap()
        .with_timeouts(Duration::from_millis(50), Duration::from_millis(50));
    let error = sink.retain(&retain_request()).unwrap_err();
    assert!(error.is_transient(), "{error}");
}

#[test]
fn health_and_delete_permanent_failure() {
    let (url, _) = support::one_shot(200, "{}");
    HindsightSink::new(url).unwrap().health().unwrap();

    let (url, _) = support::one_shot(400, "{}");
    let sink = HindsightSink::new(url).unwrap();
    let error = sink.delete_document("repo-docs", "repo--a").unwrap_err();
    assert!(!error.is_transient(), "{error}");
}

#[test]
fn malformed_and_unknown_payloads_fail_closed() {
    use chrono::Utc;
    use engram_ingest::ClaimedJob;

    let sink = HindsightSink::new("http://127.0.0.1:1").unwrap();
    let base = ClaimedJob {
        job_id: 1,
        owner: "test".into(),
        token: "token".into(),
        bank_id: "repo-docs".into(),
        kind: "retain".into(),
        payload: serde_json::json!({"document_id": "repo--a"}),
        expires_at: Utc::now(),
    };
    assert!(matches!(
        sink.dispatch("repo-docs", &base).unwrap_err(),
        engram_ingest::SinkError::Payload(_)
    ));
    let unknown = ClaimedJob {
        kind: "scan".into(),
        ..base
    };
    assert!(matches!(
        sink.dispatch("repo-docs", &unknown).unwrap_err(),
        engram_ingest::SinkError::Payload(_)
    ));
    assert!(sink.minimum_ttl().num_seconds() >= 120);
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
