//! PDF parity against the frozen Python oracle (issue #258).
//!
//! `fixtures/pdf/goldens.json` is produced by the real Python
//! `process_pdf_file` path (pdfplumber) plus `_retain_document`; see
//! `fixtures/pdf/README.md`. These tests pin the business contract:
//!
//! - rendered page markers, spacing, and blank-page omission match byte-for-byte;
//! - the shared planner gives the same document id, chunks, synthesis metadata,
//!   tags, and timestamp as Markdown does in Python.

use std::collections::BTreeMap;

use engram_ingest::{plan_document, render_pdf, ContentHash};
use serde::Deserialize;

const GOLDENS: &str = include_str!("../fixtures/pdf/goldens.json");
const FIXTURE: &[u8] = include_bytes!("../fixtures/pdf/guide.pdf");

#[derive(Debug, Deserialize)]
struct Goldens {
    provenance: Provenance,
    source_tag: String,
    relative: String,
    timestamp: Option<String>,
    pages: Vec<String>,
    rendered: String,
    expected: Vec<ExpectedCall>,
}

#[derive(Debug, Deserialize)]
struct Provenance {
    fixture: FixtureIdentity,
}

#[derive(Debug, Deserialize)]
struct FixtureIdentity {
    file: String,
    sha256: String,
    size_bytes: usize,
}

#[derive(Debug, Deserialize)]
struct ExpectedCall {
    document_id: String,
    content: String,
    metadata: BTreeMap<String, String>,
    tags: Vec<String>,
    timestamp: Option<String>,
}

#[test]
fn fixture_bytes_match_the_golden_provenance() {
    assert!(!GOLDENS.is_empty());
    let goldens: Goldens = serde_json::from_str(GOLDENS).expect("goldens");
    assert_eq!(goldens.provenance.fixture.file, "guide.pdf");
    assert_eq!(goldens.provenance.fixture.size_bytes, FIXTURE.len());
    assert_eq!(
        goldens.provenance.fixture.sha256,
        ContentHash::of(FIXTURE).to_hex(),
        "guide.pdf and goldens.json drifted; regenerate both"
    );
}

#[test]
fn rendered_pdf_matches_python_byte_for_byte() {
    let goldens: Goldens = serde_json::from_str(GOLDENS).expect("goldens");
    let rendered = render_pdf(FIXTURE).expect("render").expect("text");
    assert_eq!(rendered, goldens.rendered);
}

#[test]
fn python_oracle_keeps_blank_pages_before_omission() {
    let goldens: Goldens = serde_json::from_str(GOLDENS).expect("goldens");
    assert_eq!(goldens.pages.len(), 4);
    assert!(!goldens.pages[0].trim().is_empty());
    assert!(goldens.pages[1].trim().is_empty(), "page 2 is blank");
    assert!(!goldens.pages[2].trim().is_empty());
    assert!(goldens.pages[3].trim().is_empty(), "page 4 is spaces only");
}

#[test]
fn page_markers_keep_python_spacing_and_omit_blank_pages() {
    let rendered = render_pdf(FIXTURE).expect("render").expect("text");
    assert!(rendered.starts_with("--- Page 1 ---\n\nQuarterly Report\n"));
    assert!(rendered.contains("\n\n--- Page 3 ---\n\nSummary\n"));
    assert!(!rendered.contains("--- Page 2 ---"));
    assert!(!rendered.contains("--- Page 4 ---"));
    // pdfplumber trims line edges and collapses the paragraph gap.
    assert!(rendered.contains("Indented detail stays indented."));
    assert!(!rendered.contains("\n\n\n"));
}

#[test]
fn shared_planner_matches_python_document_ids_metadata_and_timestamp() {
    let goldens: Goldens = serde_json::from_str(GOLDENS).expect("goldens");
    let calls = plan_document(
        &goldens.source_tag,
        &goldens.relative,
        &goldens.rendered,
        goldens.timestamp.clone(),
    )
    .expect("plan");

    assert_eq!(calls.len(), goldens.expected.len());
    for (call, expected) in calls.iter().zip(&goldens.expected) {
        assert_eq!(call.document_id, expected.document_id);
        assert_eq!(call.content, expected.content);
        assert_eq!(call.metadata, expected.metadata);
        assert_eq!(call.tags, expected.tags);
        assert_eq!(call.timestamp, expected.timestamp);
        assert!(!call.content.is_empty());
    }
}

#[test]
fn pdf_metadata_uses_the_markdown_synthesis_path() {
    let goldens: Goldens = serde_json::from_str(GOLDENS).expect("goldens");
    let call = &goldens.expected[0];
    assert_eq!(call.metadata["source"], "cocoindex");
    assert_eq!(call.metadata["repo"], "manual");
    assert!(!call.metadata["keywords"].is_empty());
    assert_eq!(call.tags, vec!["root", "manual"]);
    assert_eq!(call.document_id, "manual--guide");
}
