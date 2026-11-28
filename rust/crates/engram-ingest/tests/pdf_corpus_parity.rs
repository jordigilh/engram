//! Differential PDF parity corpus (issue #258).
//!
//! The corpus goldens are produced by pdfplumber (see `fixtures/pdf/README.md`).
//! Every case must render byte-for-byte identically; a divergence is a
//! regression or a documented extractor limitation, not a silent pass.

use std::path::PathBuf;

use engram_ingest::render_pdf;
use serde::Deserialize;

const GOLDENS: &str = include_str!("../fixtures/pdf/corpus/goldens.json");

#[derive(Debug, Deserialize)]
struct Goldens {
    provenance: Provenance,
    cases: Vec<Case>,
}

#[derive(Debug, Deserialize)]
struct Provenance {
    pdfplumber: String,
}

#[derive(Debug, Deserialize)]
struct Case {
    name: String,
    file: String,
    pages: Vec<String>,
    rendered: String,
}

fn corpus_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures/pdf/corpus")
}

#[test]
fn corpus_provenance_is_pinned_to_pdfplumber() {
    let goldens: Goldens = serde_json::from_str(GOLDENS).expect("goldens");
    assert_eq!(
        goldens.provenance.pdfplumber, "0.11.10",
        "pyproject.toml pins pdfplumber; regenerate the corpus with that version"
    );
    assert!(
        goldens.cases.len() >= 8,
        "corpus must cover more than the primary fixture"
    );
}

#[test]
fn every_corpus_case_matches_pdfplumber_byte_for_byte() {
    let goldens: Goldens = serde_json::from_str(GOLDENS).expect("goldens");
    for case in &goldens.cases {
        let path = corpus_dir().join(&case.file);
        let bytes =
            std::fs::read(&path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
        let rendered = render_pdf(&bytes)
            .unwrap_or_else(|error| panic!("render {}: {error}", case.name))
            .unwrap_or_else(|| panic!("{} produced no text", case.name));
        assert_eq!(
            rendered, case.rendered,
            "{} diverged from pdfplumber",
            case.name
        );
    }
}

#[test]
fn corpus_covers_blank_pages_and_multiple_pages() {
    let goldens: Goldens = serde_json::from_str(GOLDENS).expect("goldens");
    let case = goldens
        .cases
        .iter()
        .find(|case| case.name == "multi_page_blank_middle")
        .expect("blank-middle case");
    assert_eq!(case.pages.len(), 3);
    assert!(case.pages[1].trim().is_empty());
    assert!(!case.rendered.contains("--- Page 2 ---"));
    assert!(case.rendered.contains("--- Page 3 ---"));
}
