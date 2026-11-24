//! Python-reference parity fixtures for response shaping.
//!
//! The `fixtures/parity/*.json` pairs are generated read-only from
//! `engram.pipeline.engram_gateway` by `fixtures/parity/generate.py`. Regenerate
//! them when the Python reference changes, then run this suite.

use engram_mcp::shaping::{normalize_recall_result, truncate_pattern_result};
use serde_json::Value;

macro_rules! fixture {
    ($name:literal) => {
        serde_json::from_str::<Value>(include_str!(concat!("fixtures/parity/", $name)))
            .expect(concat!("fixture ", $name, " is valid JSON"))
    };
}

fn recall_pairs() -> Vec<(&'static str, Value, Value)> {
    vec![
        (
            "bounded",
            fixture!("recall_bounded_input.json"),
            fixture!("recall_bounded_expected.json"),
        ),
        (
            "structured",
            fixture!("recall_structured_input.json"),
            fixture!("recall_structured_expected.json"),
        ),
        (
            "nulls",
            fixture!("recall_nulls_input.json"),
            fixture!("recall_nulls_expected.json"),
        ),
        (
            "source_truncated_true",
            fixture!("recall_source_truncated_true_input.json"),
            fixture!("recall_source_truncated_true_expected.json"),
        ),
        (
            "source_truncated_one",
            fixture!("recall_source_truncated_one_input.json"),
            fixture!("recall_source_truncated_one_expected.json"),
        ),
        (
            "non_object",
            fixture!("recall_non_object_input.json"),
            fixture!("recall_non_object_expected.json"),
        ),
        (
            "whitespace_summary",
            fixture!("recall_whitespace_summary_input.json"),
            fixture!("recall_whitespace_summary_expected.json"),
        ),
        (
            "non_string_text",
            fixture!("recall_non_string_text_input.json"),
            fixture!("recall_non_string_text_expected.json"),
        ),
        (
            "integer_scores",
            fixture!("recall_integer_scores_input.json"),
            fixture!("recall_integer_scores_expected.json"),
        ),
        (
            "precedence",
            fixture!("recall_precedence_input.json"),
            fixture!("recall_precedence_expected.json"),
        ),
        (
            "is_error",
            fixture!("recall_is_error_input.json"),
            fixture!("recall_is_error_expected.json"),
        ),
        (
            "falsy_labels",
            fixture!("recall_falsy_labels_input.json"),
            fixture!("recall_falsy_labels_expected.json"),
        ),
        (
            "blank_text",
            fixture!("recall_blank_text_input.json"),
            fixture!("recall_blank_text_expected.json"),
        ),
        (
            "rounding_ties",
            fixture!("recall_rounding_ties_input.json"),
            fixture!("recall_rounding_ties_expected.json"),
        ),
        (
            "bool_scores",
            fixture!("recall_bool_scores_input.json"),
            fixture!("recall_bool_scores_expected.json"),
        ),
        (
            "string_tags",
            fixture!("recall_string_tags_input.json"),
            fixture!("recall_string_tags_expected.json"),
        ),
        (
            "empty_text",
            fixture!("recall_empty_text_input.json"),
            fixture!("recall_empty_text_expected.json"),
        ),
        (
            "separator_whitespace",
            fixture!("recall_separator_whitespace_input.json"),
            fixture!("recall_separator_whitespace_expected.json"),
        ),
        (
            "empty_string_tags",
            fixture!("recall_empty_string_tags_input.json"),
            fixture!("recall_empty_string_tags_expected.json"),
        ),
    ]
}

#[test]
fn recall_matches_python_goldens() {
    for (name, input, expected) in recall_pairs() {
        assert_eq!(
            normalize_recall_result(input),
            expected,
            "recall fixture {name} diverged from Python"
        );
    }
}

#[test]
fn recall_normalization_is_idempotent_for_untruncated_summaries() {
    for (name, input) in [
        ("structured", fixture!("recall_structured_input.json")),
        (
            "whitespace_summary",
            fixture!("recall_whitespace_summary_input.json"),
        ),
    ] {
        let once = normalize_recall_result(input);
        let twice = normalize_recall_result(once.clone());
        assert_eq!(twice, once, "second normalize changed {name}");
    }
}

#[test]
fn recall_error_truthiness_passes_through() {
    let result = fixture!("recall_bounded_input.json");
    let mut error = result;
    error["isError"] = serde_json::json!(1);
    assert_eq!(normalize_recall_result(error.clone()), error);
}

#[test]
fn pattern_truncation_matches_python_goldens() {
    let cases = [
        (
            "truncate",
            fixture!("pattern_truncate_input.json"),
            20,
            fixture!("pattern_truncate_expected.json"),
        ),
        (
            "exact",
            fixture!("pattern_exact_input.json"),
            20,
            fixture!("pattern_exact_expected.json"),
        ),
        (
            "multiple",
            fixture!("pattern_multiple_input.json"),
            20,
            fixture!("pattern_multiple_expected.json"),
        ),
        (
            "non_list",
            fixture!("pattern_non_list_input.json"),
            20,
            fixture!("pattern_non_list_expected.json"),
        ),
        (
            "zero",
            fixture!("pattern_zero_input.json"),
            0,
            fixture!("pattern_zero_expected.json"),
        ),
        (
            "multibyte",
            fixture!("pattern_multibyte_input.json"),
            20,
            fixture!("pattern_multibyte_expected.json"),
        ),
        (
            "plus_one",
            fixture!("pattern_plus_one_input.json"),
            20,
            fixture!("pattern_plus_one_expected.json"),
        ),
    ];
    for (name, input, max_chars, expected) in cases {
        assert_eq!(
            truncate_pattern_result(input, max_chars),
            expected,
            "pattern fixture {name} diverged from Python"
        );
    }
}
