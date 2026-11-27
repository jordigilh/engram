//! Frozen serializer parity fixture for native transcript ingestion (issue #245).
//!
//! The golden file is produced by `fixtures/transcripts/generate_goldens.py`, a
//! line-for-line port of the documented Python contract. This test locks the
//! Rust window text, stable ids, and watermark-boundary behavior to it.

use engram_ingest::{
    is_correction, is_instruction, learning_windows, parse_messages, window_document_id,
};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct GoldenFile {
    fixture: String,
    message_count: usize,
    cases: Vec<GoldenCase>,
}

#[derive(Debug, Deserialize)]
struct GoldenCase {
    start_index: usize,
    windows: Vec<GoldenWindow>,
}

#[derive(Debug, Deserialize)]
struct GoldenWindow {
    text: String,
    document_id: String,
    signal_raw_index: usize,
}

#[test]
fn windows_match_the_python_contract_fixture() {
    let content = include_str!("../fixtures/transcripts/session.jsonl");
    let goldens: GoldenFile =
        serde_json::from_str(include_str!("../fixtures/transcripts/goldens.json")).unwrap();
    assert_eq!(goldens.fixture, "session.jsonl");

    let messages = parse_messages(content.as_bytes()).unwrap();
    assert_eq!(messages.len(), goldens.message_count);

    for case in &goldens.cases {
        let windows = learning_windows(&messages, case.start_index);
        assert_eq!(
            windows.len(),
            case.windows.len(),
            "unexpected window count at start_index {}",
            case.start_index
        );
        for (actual, expected) in windows.iter().zip(&case.windows) {
            assert_eq!(actual.text, expected.text);
            assert_eq!(actual.signal_raw_index, expected.signal_raw_index);
            assert_eq!(
                window_document_id("session", &actual.text),
                expected.document_id
            );
        }
    }
}

#[test]
fn signal_gates_match_the_python_regex_contract() {
    assert!(is_correction("No, that's wrong; use the other approach."));
    assert!(is_correction("we don't use that endpoint here"));
    assert!(!is_correction("please add a new endpoint"));
    assert!(!is_correction(
        "The beginning of the above subagent result is already visible to the user."
    ));
    assert!(is_instruction("Always use tests before proceeding."));
    assert!(is_instruction("we never deploy on Friday"));
    assert!(!is_instruction("too short"));
}

#[test]
fn unicode_truncation_is_character_based() {
    let long = "é".repeat(320);
    let message = serde_json::json!({
        "role": "user",
        "message": {"content": format!("always use this {long}")}
    });
    let windows = learning_windows(&[message], 0);
    assert_eq!(windows.len(), 1);
    let line = windows[0]
        .text
        .lines()
        .find(|line| line.starts_with("[INSTRUCTION] User:"))
        .unwrap();
    let text = line.trim_start_matches("[INSTRUCTION] User: ");
    assert_eq!(text.chars().count(), 300);
}
