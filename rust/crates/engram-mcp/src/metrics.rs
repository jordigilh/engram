//! Per-call gateway metrics ported from `engram_gateway.py`.
//!
//! Every `tools/call` that reaches a backend appends one JSON line to
//! `gateway-calls.jsonl`. The log is observational: a missing or unwritable
//! path degrades to a stderr warning and must never fail the call it observes.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use chrono::Utc;
use serde::Serialize;
use serde_json::Value;

use crate::shaping::json_truthy;

/// Environment override for the metrics file path.
pub const METRICS_LOG_ENV: &str = "ENGRAM_METRICS_LOG";

#[derive(Debug, Serialize, PartialEq, Eq)]
struct GatewayCall {
    ts: String,
    project: String,
    backend: String,
    tool: String,
    is_error: bool,
    result_chars: usize,
    est_tokens: usize,
}

/// Append-only sink for `gateway-calls.jsonl`.
#[derive(Debug, Clone)]
pub struct MetricsLog {
    path: PathBuf,
}

impl MetricsLog {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    /// `$ENGRAM_METRICS_LOG`, else `$HOME/.engram/logs/gateway-calls.jsonl`.
    pub fn from_environment() -> Option<Self> {
        if let Some(path) = std::env::var_os(METRICS_LOG_ENV) {
            let path = PathBuf::from(path);
            if !path.as_os_str().is_empty() {
                return Some(Self::new(path));
            }
        }
        let home = std::env::var_os("HOME")?;
        Some(Self::new(
            PathBuf::from(home).join(".engram/logs/gateway-calls.jsonl"),
        ))
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Log a successful backend result using the same text extraction and
    /// token estimate Python applies after response shaping.
    pub fn record_result(&self, project: &str, backend: &str, tool: &str, result: &Value) {
        let text = extract_result_text(result);
        let entry = GatewayCall {
            ts: timestamp(),
            project: project.to_string(),
            backend: backend.to_string(),
            tool: tool.to_string(),
            is_error: json_truthy(result.get("isError")),
            result_chars: text.chars().count(),
            est_tokens: estimate_tokens(&text),
        };
        self.append(&entry);
    }

    /// Python logs a failed backend call with zero chars/tokens: the response
    /// never existed, so there is nothing to count.
    pub fn record_error(&self, project: &str, backend: &str, tool: &str) {
        let entry = GatewayCall {
            ts: timestamp(),
            project: project.to_string(),
            backend: backend.to_string(),
            tool: tool.to_string(),
            is_error: true,
            result_chars: 0,
            est_tokens: 0,
        };
        self.append(&entry);
    }

    fn append(&self, entry: &GatewayCall) {
        let line = match serde_json::to_string(entry) {
            Ok(line) => line,
            Err(error) => {
                eprintln!("engram-mcp: cannot serialize gateway call metric: {error}");
                return;
            }
        };
        if let Some(parent) = self.path.parent() {
            if let Err(error) = std::fs::create_dir_all(parent) {
                eprintln!(
                    "engram-mcp: failed to create gateway metrics dir {}: {error}",
                    parent.display()
                );
                return;
            }
        }
        let write = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .and_then(|mut file| writeln!(file, "{line}"));
        if let Err(error) = write {
            eprintln!(
                "engram-mcp: failed to write gateway call metrics to {}: {error}",
                self.path.display()
            );
        }
    }
}

fn timestamp() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%S").to_string()
}

/// Concatenate every `type: "text"` content item, matching Python's
/// `_extract_result_text`. Non-text content contributes nothing.
pub fn extract_result_text(result: &Value) -> String {
    let Some(content) = result.get("content").and_then(Value::as_array) else {
        return String::new();
    };
    let mut text = String::new();
    for item in content {
        if item.get("type").and_then(Value::as_str) != Some("text") {
            continue;
        }
        if let Some(part) = item.get("text").and_then(Value::as_str) {
            text.push_str(part);
        }
    }
    text
}

/// Best-effort `cl100k_base` token count with Python's chars/4 fallback.
pub fn estimate_tokens(text: &str) -> usize {
    if text.is_empty() {
        return 0;
    }
    match encoding() {
        Some(encoding) => encoding.encode_ordinary(text).len(),
        None => text.chars().count() / 4,
    }
}

fn encoding() -> Option<&'static tiktoken_rs::CoreBPE> {
    static ENCODING: OnceLock<Option<tiktoken_rs::CoreBPE>> = OnceLock::new();
    ENCODING
        .get_or_init(|| {
            tiktoken_rs::cl100k_base()
                .map_err(|error| {
                    eprintln!("engram-mcp: tiktoken initialization failed, using chars/4: {error}");
                    error
                })
                .ok()
        })
        .as_ref()
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn temp_path(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("engram-metrics-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir.join("gateway-calls.jsonl")
    }

    #[test]
    fn extracts_only_text_items_in_order() {
        let result = json!({
            "content": [
                {"type": "text", "text": "alpha"},
                {"type": "image", "data": "..."},
                {"type": "text", "text": "beta"},
                {"type": "text"}
            ]
        });
        assert_eq!(extract_result_text(&result), "alphabeta");
        assert_eq!(extract_result_text(&json!({})), "");
    }

    #[test]
    fn empty_text_is_zero_tokens() {
        assert_eq!(estimate_tokens(""), 0);
    }

    #[test]
    fn token_estimate_uses_cl100k_for_known_text() {
        assert_eq!(estimate_tokens("hello world"), 2);
        assert!(estimate_tokens(&"token ".repeat(100)) > 50);
    }

    #[test]
    fn result_chars_counts_characters_not_bytes() {
        let log = MetricsLog::new(temp_path("chars"));
        log.record_result(
            "demo",
            "docs",
            "demo_recall",
            &json!({"content": [{"type": "text", "text": "héllo"}]}),
        );
        let line = std::fs::read_to_string(log.path()).expect("log");
        let entry: serde_json::Value = serde_json::from_str(line.trim()).expect("json");
        assert_eq!(entry["result_chars"], 5);
        assert_eq!(entry["project"], "demo");
        assert_eq!(entry["backend"], "docs");
        assert_eq!(entry["tool"], "demo_recall");
        assert_eq!(entry["is_error"], false);
        assert_eq!(entry["est_tokens"], 3);
    }

    #[test]
    fn error_rows_are_zeroed_and_flagged() {
        let log = MetricsLog::new(temp_path("error"));
        log.record_error("demo", "code", "demo_search");
        let line = std::fs::read_to_string(log.path()).expect("log");
        let entry: serde_json::Value = serde_json::from_str(line.trim()).expect("json");
        assert_eq!(entry["is_error"], true);
        assert_eq!(entry["result_chars"], 0);
        assert_eq!(entry["est_tokens"], 0);
    }

    #[test]
    fn unwritable_path_does_not_panic() {
        let log = MetricsLog::new("/dev/null/not-a-dir/gateway-calls.jsonl");
        log.record_result(
            "demo",
            "docs",
            "demo_recall",
            &json!({"content": [{"type": "text", "text": "ok"}]}),
        );
    }

    #[test]
    fn is_error_uses_python_truthiness() {
        let log = MetricsLog::new(temp_path("truthy"));
        for (index, is_error) in [json!(1), json!("yes"), json!([]), json!(0)]
            .into_iter()
            .enumerate()
        {
            log.record_result(
                "demo",
                "docs",
                &format!("tool{index}"),
                &json!({"isError": is_error, "content": []}),
            );
        }
        let text = std::fs::read_to_string(log.path()).expect("log");
        let rows: Vec<serde_json::Value> = text
            .lines()
            .map(|line| serde_json::from_str(line).expect("json"))
            .collect();
        assert_eq!(
            rows.iter()
                .map(|row| row["is_error"].clone())
                .collect::<Vec<_>>(),
            vec![json!(true), json!(true), json!(false), json!(false)]
        );
    }
}
