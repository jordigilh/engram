//! PDF text extraction for the docs runtime (issue #258).
//!
//! Behavior mirrors `process_pdf_file` in `src/engram/flows/configured.py`:
//!
//! - every page keeps its 1-based number, so a blank page leaves a gap in the
//!   rendered markers instead of renumbering later pages;
//! - a page participates only when its text is non-whitespace;
//! - each participating page renders as `--- Page {n} ---\n\n{text}` and pages
//!   are joined with `\n\n`;
//! - a document whose rendered text is whitespace-only yields `None`, which the
//!   caller turns into "no retain calls" (the Python early return).
//!
//! pdfplumber's `extract_text` emits one entry per visual line, strips line
//! edges, and never emits empty lines. `pdf-extract` keeps line indentation and
//! paragraph gaps, so [`normalize_page`] applies the same line shape before the
//! page is rendered. The parity fixture in `fixtures/pdf` pins the resulting
//! content byte-for-byte against the Python oracle.
//!
//! Only text-bearing PDFs are supported. Scanned/image-only PDFs produce no
//! text in both implementations; OCR is explicitly out of scope (issue #258).

use std::panic::{catch_unwind, AssertUnwindSafe};

#[derive(Debug, thiserror::Error)]
pub enum PdfError {
    #[error("cannot extract PDF text: {0}")]
    Extract(String),
    #[error("PDF text extraction panicked on malformed input")]
    Panic,
}

/// Extract and render one PDF from bytes already read by the scanner.
///
/// `None` means the document has no non-whitespace text (Python's
/// `process_pdf_file` returns without retaining). Any parse/extraction failure
/// is an error so the caller aborts the whole source snapshot instead of
/// tombstoning documents from a partial view.
pub fn render_pdf(bytes: &[u8]) -> Result<Option<String>, PdfError> {
    let pages = guarded(|| pdf_extract::extract_text_from_mem_by_pages(bytes))?
        .map_err(|error| PdfError::Extract(error.to_string()))?;
    Ok(render_pages(&pages))
}

/// Render already-extracted pages with the Python page-marker contract.
pub fn render_pages(pages: &[String]) -> Option<String> {
    let rendered = pages
        .iter()
        .enumerate()
        .filter_map(|(index, page)| {
            let text = normalize_page(page);
            (!text.is_empty()).then(|| format!("--- Page {} ---\n\n{text}", index + 1))
        })
        .collect::<Vec<_>>()
        .join("\n\n");
    (!rendered.trim().is_empty()).then_some(rendered)
}

/// Match pdfplumber's line shape: trimmed line edges, no empty lines.
fn normalize_page(page: &str) -> String {
    page.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

/// Keep a malformed PDF from panicking the ingest process. The extractor is
/// third-party code parsing untrusted file structure; the runtime must turn a
/// panic into a source-level error, not an aborted worker.
fn guarded<T>(operation: impl FnOnce() -> T) -> Result<T, PdfError> {
    catch_unwind(AssertUnwindSafe(operation)).map_err(|_| PdfError::Panic)
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &[u8] = include_bytes!("../fixtures/pdf/guide.pdf");

    #[test]
    fn renders_page_markers_and_omits_blank_pages_without_renumbering() {
        let rendered = render_pdf(FIXTURE).expect("fixture").expect("text");
        assert!(rendered.starts_with("--- Page 1 ---\n\n"));
        assert!(rendered.contains("\n\n--- Page 3 ---\n\n"));
        assert!(!rendered.contains("--- Page 2 ---"));
        assert!(!rendered.contains("--- Page 4 ---"));
    }

    #[test]
    fn keeps_non_ascii_and_escaped_delimiters() {
        let rendered = render_pdf(FIXTURE).expect("fixture").expect("text");
        assert!(rendered.contains("Café"));
        assert!(rendered.contains("résumé – naïve façade"));
        assert!(rendered.contains("R&D (draft) review"));
    }

    #[test]
    fn normalizes_lines_like_pdfplumber() {
        assert_eq!(normalize_page("\n  Indented  \n\nnext\n"), "Indented\nnext");
        assert_eq!(normalize_page("   \n\t\n"), "");
        assert_eq!(normalize_page("a\r\nb\r\n"), "a\nb");
    }

    #[test]
    fn whitespace_only_pages_render_nothing() {
        assert_eq!(render_pages(&[]), None);
        assert_eq!(render_pages(&["".to_string(), " \n\t".to_string()]), None);
    }

    #[test]
    fn page_numbers_are_preserved_across_whitespace_pages() {
        let pages = vec!["one".to_string(), " ".to_string(), "three".to_string()];
        assert_eq!(
            render_pages(&pages).expect("content"),
            "--- Page 1 ---\n\none\n\n--- Page 3 ---\n\nthree"
        );
    }

    #[test]
    fn malformed_pdf_is_an_error_not_a_panic() {
        let error = render_pdf(b"not a pdf").expect_err("invalid");
        assert!(matches!(error, PdfError::Extract(_)));
        assert!(render_pdf(b"").is_err());
    }

    #[test]
    fn panic_boundary_reports_panic() {
        let error = guarded(|| panic!("boom")).expect_err("panic");
        assert!(matches!(error, PdfError::Panic));
    }
}
