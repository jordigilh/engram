//! Client-facing response shaping ported from `engram_gateway.py`.
//!
//! The gateway keeps upstream MCP payloads intact except for the two
//! deterministic, fail-open transforms Python applies today: bounded recall
//! envelopes and `search_for_pattern` truncation.
//!
//! Object-valued `tags` render keys in sorted order because `serde_json` maps
//! are ordered; Python preserves dict insertion order. Upstream Hindsight
//! sends tags as arrays (or strings), so this only affects hypothetical
//! object-valued tags.

use std::borrow::Cow;

use serde_json::{json, Map, Value};

pub const RECALL_SCHEMA_VERSION: &str = "engram-recall.v1";
pub const MAX_RECALL_RESULTS: usize = 8;
pub const MAX_RECALL_SUMMARY_CHARS: usize = 800;
pub const MAX_RECALL_METADATA_VALUE_CHARS: usize = 240;

/// Normalize a Hindsight `recall` result into the bounded `engram-recall.v1`
/// shape. Anything that is not a normal recall envelope passes through
/// untouched so unfamiliar backend output stays diagnosable.
pub fn normalize_recall_result(result: Value) -> Value {
    if json_truthy(result.get("isError")) {
        return result;
    }
    let Some(payload) = recall_payload(&result) else {
        return result;
    };
    let empty = Vec::new();
    let raw_records = payload
        .get("results")
        .and_then(Value::as_array)
        .unwrap_or(&empty);

    let records: Vec<Value> = raw_records
        .iter()
        .take(MAX_RECALL_RESULTS)
        .filter_map(normalize_recall_record)
        .collect();
    let source_truncated = json_truthy(payload.get("source_facts_truncated"));
    let total_results = raw_records.len();
    let returned_results = records.len();

    let mut structured = Map::new();
    structured.insert("schema_version".into(), json!(RECALL_SCHEMA_VERSION));
    structured.insert("results".into(), Value::Array(records.clone()));
    structured.insert("total_results".into(), json!(total_results));
    structured.insert("returned_results".into(), json!(returned_results));
    structured.insert(
        "truncated".into(),
        json!(total_results > returned_results || source_truncated),
    );
    if source_truncated {
        structured.insert("source_facts_truncated".into(), json!(true));
    }

    let rendered = format_recall_text(&records, total_results);
    let mut normalized = result;
    if let Some(object) = normalized.as_object_mut() {
        object.insert(
            "content".into(),
            json!([{ "type": "text", "text": rendered }]),
        );
        object.insert("structuredContent".into(), Value::Object(structured));
    }
    normalized
}

fn recall_payload(result: &Value) -> Option<Cow<'_, Value>> {
    if let Some(structured) = result.get("structuredContent") {
        if structured.get("results").is_some_and(Value::is_array) {
            return Some(Cow::Borrowed(structured));
        }
    }
    for item in result.get("content").and_then(Value::as_array)? {
        if item.get("type").and_then(Value::as_str) != Some("text") {
            continue;
        }
        let Some(text) = item.get("text").and_then(Value::as_str) else {
            continue;
        };
        let Ok(payload) = serde_json::from_str::<Value>(text) else {
            continue;
        };
        if payload.get("results").is_some_and(Value::is_array) {
            return Some(Cow::Owned(payload));
        }
    }
    None
}

fn normalize_recall_record(record: &Value) -> Option<Value> {
    let record = record.as_object()?;
    let mut normalized = Map::new();

    for key in [
        "id",
        "fact_type",
        "context",
        "occurred_start",
        "occurred_end",
        "mentioned_at",
        "document_id",
        "tags",
    ] {
        if let Some(value) = record.get(key).filter(|value| !value.is_null()) {
            normalized.insert(key.to_string(), value.clone());
        }
    }

    let mut summary = [
        record.get("summary"),
        record
            .get("metadata")
            .and_then(|metadata| metadata.get("key_sentences")),
    ]
    .into_iter()
    .flatten()
    .find_map(|value| {
        value
            .as_str()
            .filter(|text| !py_strip(text).is_empty())
            .map(str::to_string)
    });
    // Python's final `text` fallback accepts any string, including blank.
    if summary.is_none() {
        summary = record
            .get("text")
            .and_then(Value::as_str)
            .map(str::to_string);
    }
    if let Some(summary) = summary {
        normalized.insert(
            "summary".into(),
            Value::String(bounded_text(&summary, MAX_RECALL_SUMMARY_CHARS)),
        );
    }

    if let Some(metadata) = record.get("metadata").and_then(Value::as_object) {
        if let Some(keywords) = metadata.get("keywords").and_then(Value::as_str) {
            let keywords: Vec<Value> = keywords
                .split(',')
                .map(py_strip)
                .filter(|keyword| !keyword.is_empty())
                .map(|keyword| Value::String(keyword.to_string()))
                .collect();
            if !keywords.is_empty() {
                normalized.insert("keywords".into(), Value::Array(keywords));
            }
        }

        let mut compact = Map::new();
        for (key, value) in metadata {
            if key == "key_sentences" || key == "keywords" {
                continue;
            }
            let value = match value {
                Value::String(text) => {
                    Value::String(bounded_text(text, MAX_RECALL_METADATA_VALUE_CHARS))
                }
                Value::Number(_) | Value::Bool(_) => value.clone(),
                _ => continue,
            };
            compact.insert(key.clone(), value);
        }
        if !compact.is_empty() {
            normalized.insert("metadata".into(), Value::Object(compact));
        }
    }

    if let Some(scores) = record.get("scores").and_then(Value::as_object) {
        let mut compact = Map::new();
        for (key, value) in scores {
            match value {
                Value::Null => continue,
                Value::Number(number) => {
                    let rounded = if let Some(value) = number.as_i64() {
                        json!(value)
                    } else if let Some(value) = number.as_u64() {
                        json!(value)
                    } else {
                        let value = number.as_f64().unwrap_or_default();
                        let text = format!("{value:.4}");
                        text.parse::<f64>()
                            .map_or_else(|_| json!(value), |rounded| json!(rounded))
                    };
                    compact.insert(key.clone(), rounded);
                }
                Value::Bool(value) => {
                    compact.insert(key.clone(), json!(i64::from(*value)));
                }
                other => {
                    compact.insert(key.clone(), other.clone());
                }
            }
        }
        if !compact.is_empty() {
            normalized.insert("scores".into(), Value::Object(compact));
        }
    }

    Some(Value::Object(normalized))
}

fn format_recall_text(records: &[Value], total: usize) -> String {
    let mut lines = vec![format!(
        "Engram recall ({} of {} results; schema {})",
        records.len(),
        total,
        RECALL_SCHEMA_VERSION
    )];
    for (index, record) in records.iter().enumerate() {
        let mut labels = Vec::new();
        for key in ["fact_type", "context"] {
            if let Some(value) = record.get(key).filter(|value| json_truthy(Some(value))) {
                labels.push(py_str(value));
            }
        }
        if let Some(document_id) = record
            .get("document_id")
            .filter(|value| json_truthy(Some(value)))
        {
            labels.push(format!("document={}", py_str(document_id)));
        }
        if let Some(tags) = record.get("tags").filter(|value| json_truthy(Some(value))) {
            let rendered = match tags {
                Value::Array(tags) => tags.iter().map(py_str).collect::<Vec<_>>().join(","),
                Value::String(tags) => tags
                    .chars()
                    .map(|character| character.to_string())
                    .collect::<Vec<_>>()
                    .join(","),
                Value::Object(tags) => tags.keys().cloned().collect::<Vec<_>>().join(","),
                other => py_str(other),
            };
            // Python appends the label whenever `tags` is truthy, including
            // an array of empty strings (`tags=`).
            labels.push(format!("tags={rendered}"));
        }
        lines.push(format!(
            "\n[{}] {}",
            index + 1,
            if labels.is_empty() {
                "memory".to_string()
            } else {
                labels.join(" | ")
            }
        ));
        if let Some(summary) = record
            .get("summary")
            .and_then(Value::as_str)
            .filter(|summary| !summary.is_empty())
        {
            lines.push(summary.to_string());
        }
    }
    if total > records.len() {
        lines.push(format!(
            "\n... {} lower-ranked results omitted; narrow the query for more detail.",
            total - records.len()
        ));
    }
    lines.join("\n")
}

fn bounded_text(value: &str, limit: usize) -> String {
    if value.len() <= limit {
        return value.to_string();
    }
    let Some((cut, _)) = value.char_indices().nth(limit) else {
        return value.to_string();
    };
    format!(
        "{}\n... [truncated; {} chars total]",
        py_rstrip(&value[..cut]),
        value.chars().count()
    )
}

/// Python `str.isspace()` includes the C0 separators U+001C-U+001F, which
/// Rust's `char::is_whitespace` does not.
fn py_is_space(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

fn py_strip(value: &str) -> &str {
    value.trim_matches(py_is_space)
}

fn py_rstrip(value: &str) -> &str {
    value.trim_end_matches(py_is_space)
}

/// Truncate `search_for_pattern` text content to `max_answer_chars`.
pub fn truncate_pattern_result(mut result: Value, max_chars: i64) -> Value {
    if max_chars <= 0 {
        return result;
    }
    let Some(content) = result.get_mut("content").and_then(Value::as_array_mut) else {
        return result;
    };
    for item in content {
        let Some(object) = item.as_object_mut() else {
            continue;
        };
        let Some(text) = object.get_mut("text").and_then(|value| match value {
            Value::String(text) => Some(text),
            _ => None,
        }) else {
            continue;
        };
        let limit = usize::try_from(max_chars).unwrap_or(usize::MAX);
        if limit >= text.len() {
            continue;
        }
        if let Some((cut, _)) = text.char_indices().nth(limit) {
            *text = format!(
                "{}\n... truncated at max_answer_chars={max_chars}",
                &text[..cut]
            );
        }
    }
    result
}

/// Python `int(arguments.get("max_answer_chars") or -1)` coercion, made
/// fail-open for non-numeric input instead of raising after the backend call.
pub fn coerce_max_answer_chars(value: Option<&Value>) -> i64 {
    coerce_int_or(value, -1)
}

/// Python `int(value or default)`, fail-open for non-numeric values.
pub(crate) fn coerce_int_or(value: Option<&Value>, default: i64) -> i64 {
    let Some(value) = value.filter(|value| json_truthy(Some(*value))) else {
        return default;
    };
    match value {
        Value::Bool(true) => 1,
        Value::Number(number) => number
            .as_i64()
            .or_else(|| {
                number
                    .as_u64()
                    .map(|value| value.min(i64::MAX as u64) as i64)
            })
            .or_else(|| number.as_f64().map(|value| value.trunc() as i64))
            .unwrap_or(default),
        Value::String(value) => parse_python_int(value).unwrap_or(default),
        _ => default,
    }
}

/// Python's `int()` uses Unicode `White_Space` (not `str.isspace()`, which
/// also treats U+001C-U+001F as whitespace) and accepts non-ASCII decimal
/// digits. This parser is ASCII-only by design (documented divergence,
/// fail-open to `-1`).
fn parse_python_int(value: &str) -> Option<i64> {
    let trimmed = value.trim_matches(char::is_whitespace);
    let characters: Vec<char> = trimmed.chars().collect();
    if characters.is_empty() {
        return None;
    }
    for (index, character) in characters.iter().enumerate() {
        if *character != '_' {
            continue;
        }
        let previous = index.checked_sub(1).and_then(|index| characters.get(index));
        let next = characters.get(index + 1);
        if !previous.is_some_and(|value| value.is_ascii_digit())
            || !next.is_some_and(|value| value.is_ascii_digit())
        {
            return None;
        }
    }
    trimmed.replace('_', "").parse::<i64>().ok()
}

pub(crate) fn json_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_f64().is_some_and(|value| value != 0.0),
        Some(Value::String(value)) => !value.is_empty(),
        Some(Value::Array(value)) => !value.is_empty(),
        Some(Value::Object(value)) => !value.is_empty(),
    }
}

fn py_str(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Bool(value) => {
            if *value {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Null => "None".to_string(),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_text_uses_character_counts() {
        let text = "é".repeat(10);
        let bounded = bounded_text(&text, 4);
        assert_eq!(bounded, "éééé\n... [truncated; 10 chars total]");
        assert_eq!(bounded_text("short", 10), "short");
        assert_eq!(
            bounded_text("\u{1c}abc", 1),
            "\n... [truncated; 4 chars total]"
        );
        assert_eq!(py_strip("\u{1c} x \u{1f}"), "x");
    }

    #[test]
    fn max_answer_chars_coercion() {
        assert_eq!(coerce_max_answer_chars(None), -1);
        assert_eq!(coerce_max_answer_chars(Some(&json!(null))), -1);
        assert_eq!(coerce_max_answer_chars(Some(&json!(0))), -1);
        assert_eq!(coerce_max_answer_chars(Some(&json!(false))), -1);
        assert_eq!(coerce_max_answer_chars(Some(&json!(true))), 1);
        assert_eq!(coerce_max_answer_chars(Some(&json!(20))), 20);
        assert_eq!(coerce_max_answer_chars(Some(&json!(20.9))), 20);
        assert_eq!(coerce_max_answer_chars(Some(&json!("20"))), 20);
        assert_eq!(coerce_max_answer_chars(Some(&json!("1_000"))), 1000);
        assert_eq!(coerce_max_answer_chars(Some(&json!("_20"))), -1);
        assert_eq!(coerce_max_answer_chars(Some(&json!("2__0"))), -1);
        assert_eq!(coerce_max_answer_chars(Some(&json!("\u{1c}20"))), -1);
        assert_eq!(coerce_max_answer_chars(Some(&json!("abc"))), -1);
        assert_eq!(coerce_max_answer_chars(Some(&json!([20]))), -1);
    }

    #[test]
    fn truncation_ignores_non_positive_and_huge_limits() {
        let result = json!({"content": [{"type": "text", "text": "x".repeat(50)}]});
        assert_eq!(truncate_pattern_result(result.clone(), -1), result);
        assert_eq!(truncate_pattern_result(result.clone(), 0), result);
        assert_eq!(truncate_pattern_result(result.clone(), i64::MAX), result);
    }

    #[test]
    fn is_error_truthiness_passes_through() {
        let result = json!({
            "content": [{"type": "text", "text": "{\"results\":[{\"text\":\"x\"}]}"}],
            "isError": 1
        });
        assert_eq!(normalize_recall_result(result.clone()), result);
    }

    #[test]
    fn non_recall_and_malformed_payloads_pass_through() {
        let non_recall =
            json!({"content": [{"type": "text", "text": "{\"items\":[1]}"}], "isError": false});
        assert_eq!(normalize_recall_result(non_recall.clone()), non_recall);
        let malformed =
            json!({"content": [{"type": "text", "text": "not JSON"}], "isError": false});
        assert_eq!(normalize_recall_result(malformed.clone()), malformed);
    }
}
