//! Parity against frozen fixtures produced by the Python docs flow
//! (Python 3.14.6, sklearn 1.9.1, networkx 3.7). See `fixtures/README.md`.

use std::collections::BTreeMap;

use engram_ingest::{
    analyze, document_base_id, plan_document, split_markdown_sections, synthesize_document,
};
use serde::Deserialize;

const GOLDENS: &str = include_str!("../fixtures/goldens.json");

#[derive(Debug, Deserialize)]
struct Goldens {
    provenance: Provenance,
    base_ids: Vec<BaseIdCase>,
    chunking: Vec<ChunkingCase>,
    documents: Vec<DocumentCase>,
    synthesis: Vec<SynthesisCase>,
}

#[derive(Debug, Deserialize)]
struct Provenance {
    python: String,
    sklearn: String,
    networkx: String,
}

#[derive(Debug, Deserialize)]
struct BaseIdCase {
    source_tag: String,
    relative: String,
    expected: String,
}

#[derive(Debug, Deserialize)]
struct ChunkingCase {
    name: String,
    content: String,
    chunk_size: usize,
    chunk_overlap: usize,
    expected: Vec<(String, String)>,
}

#[derive(Debug, Deserialize)]
struct DocumentCase {
    name: String,
    source_tag: String,
    relative: String,
    content: String,
    timestamp: Option<String>,
    expected: Vec<ExpectedCall>,
}

#[derive(Debug, Deserialize)]
struct ExpectedCall {
    document_id: String,
    content: String,
    metadata: BTreeMap<String, String>,
    tags: Vec<String>,
    timestamp: Option<String>,
}

#[derive(Debug, Deserialize)]
struct SynthesisCase {
    name: String,
    doc_id: String,
    text: String,
    expected: ExpectedSynthesis,
}

#[derive(Debug, Deserialize)]
struct ExpectedSynthesis {
    key_sentences: Vec<String>,
    keywords: Vec<String>,
    stats: ExpectedStats,
    analysis: ExpectedAnalysis,
}

#[derive(Debug, Deserialize)]
struct ExpectedStats {
    n_sentences: usize,
    source_chars: usize,
    coverage_chars: usize,
}

#[derive(Debug, Deserialize)]
struct ExpectedAnalysis {
    sentences: Vec<String>,
    vocabulary: Vec<String>,
    n_keep: usize,
    tfidf: Vec<Vec<f64>>,
    row_sums: Vec<f64>,
    ranks: Vec<f64>,
    keyword_scores: Vec<f64>,
    key_indices: Vec<usize>,
    keyword_indices: Vec<usize>,
    empty_vocabulary: bool,
}

fn goldens() -> Goldens {
    let goldens: Goldens = serde_json::from_str(GOLDENS).expect("goldens parse");
    assert!(
        goldens.provenance.python.starts_with("3.14"),
        "goldens were generated with Python {}, expected 3.14.x",
        goldens.provenance.python
    );
    assert_eq!(goldens.provenance.sklearn, "1.9.1");
    assert_eq!(goldens.provenance.networkx, "3.7");
    goldens
}

fn assert_close(actual: &[f64], expected: &[f64], tolerance: f64, context: &str) {
    assert_eq!(actual.len(), expected.len(), "length mismatch: {context}");
    for (index, (actual, expected)) in actual.iter().zip(expected).enumerate() {
        let difference = (actual - expected).abs();
        assert!(
            difference <= tolerance,
            "{context}[{index}] differs: actual={actual} expected={expected} diff={difference}"
        );
    }
}

#[test]
fn base_ids_match_python() {
    for case in goldens().base_ids {
        assert_eq!(
            document_base_id(&case.source_tag, &case.relative),
            case.expected,
            "base id for {} / {}",
            case.source_tag,
            case.relative
        );
    }
}

#[test]
fn chunking_matches_python() {
    for case in goldens().chunking {
        assert_eq!(
            split_markdown_sections(&case.content, case.chunk_size, case.chunk_overlap),
            case.expected,
            "chunking case {}",
            case.name
        );
    }
}

#[test]
fn document_plans_match_python() {
    for case in goldens().documents {
        let calls = plan_document(
            &case.source_tag,
            &case.relative,
            &case.content,
            case.timestamp.clone(),
        )
        .unwrap();
        assert_eq!(calls.len(), case.expected.len(), "case {}", case.name);
        for (actual, expected) in calls.iter().zip(&case.expected) {
            assert_eq!(
                actual.document_id, expected.document_id,
                "case {}",
                case.name
            );
            assert_eq!(actual.content, expected.content, "case {}", case.name);
            assert_eq!(actual.metadata, expected.metadata, "case {}", case.name);
            assert_eq!(actual.tags, expected.tags, "case {}", case.name);
            assert_eq!(actual.timestamp, expected.timestamp, "case {}", case.name);
        }
    }
}

#[test]
fn synthesis_matches_python() {
    for case in goldens().synthesis {
        let actual = synthesize_document(&case.doc_id, &case.text)
            .unwrap_or_else(|error| panic!("case {}: {error}", case.name));
        assert_eq!(
            actual.key_sentences, case.expected.key_sentences,
            "key sentences for {}",
            case.name
        );
        assert_eq!(
            actual.keywords, case.expected.keywords,
            "keywords for {}",
            case.name
        );
        assert_eq!(actual.stats.n_sentences, case.expected.stats.n_sentences);
        assert_eq!(actual.stats.source_chars, case.expected.stats.source_chars);
        assert_eq!(
            actual.stats.coverage_chars,
            case.expected.stats.coverage_chars
        );
    }
}

#[test]
fn synthesis_intermediates_match_python() {
    for case in goldens().synthesis {
        let expected = case.expected.analysis;
        let actual =
            analyze(&case.text).unwrap_or_else(|error| panic!("case {}: {error}", case.name));
        let context = &case.name;

        assert_eq!(actual.sentences, expected.sentences, "sentences: {context}");
        assert_eq!(actual.vocabulary, expected.vocabulary, "vocab: {context}");
        assert_eq!(actual.n_keep, expected.n_keep, "n_keep: {context}");
        assert_eq!(
            actual.empty_vocabulary, expected.empty_vocabulary,
            "empty vocab: {context}"
        );
        assert_eq!(actual.key_indices, expected.key_indices, "keys: {context}");
        assert_eq!(
            actual.keyword_indices, expected.keyword_indices,
            "keywords: {context}"
        );
        assert_eq!(actual.tfidf.len(), expected.tfidf.len(), "rows: {context}");
        for (row, (actual, expected)) in actual.tfidf.iter().zip(&expected.tfidf).enumerate() {
            let expected_sparse: Vec<(usize, f64)> = expected
                .iter()
                .enumerate()
                .filter(|(_, value)| **value != 0.0)
                .map(|(index, value)| (index, *value))
                .collect();
            assert_eq!(
                actual.iter().map(|(index, _)| *index).collect::<Vec<_>>(),
                expected_sparse
                    .iter()
                    .map(|(index, _)| *index)
                    .collect::<Vec<_>>(),
                "{context} tfidf columns row {row}"
            );
            assert_close(
                &actual.iter().map(|(_, value)| *value).collect::<Vec<_>>(),
                &expected_sparse
                    .iter()
                    .map(|(_, value)| *value)
                    .collect::<Vec<_>>(),
                1.0e-9,
                &format!("{context} tfidf row {row}"),
            );
        }
        assert_close(&actual.row_sums, &expected.row_sums, 1.0e-9, context);
        assert_close(&actual.ranks, &expected.ranks, 1.0e-6, context);
        assert_close(
            &actual.keyword_scores,
            &expected.keyword_scores,
            1.0e-9,
            context,
        );
    }
}

#[test]
fn single_sentence_document_uses_row_mass_fallback() {
    // A one-node graph has no edges, so this verifies the fallback path does
    // not produce a convergence error for documents Python can synthesize.
    let result = synthesize_document(
        "doc",
        "Only one long enough sentence exists in this document.",
    );
    assert!(result.is_ok());
}
