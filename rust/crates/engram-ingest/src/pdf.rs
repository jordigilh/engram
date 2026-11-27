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

use std::io::Read;
use std::panic::{catch_unwind, AssertUnwindSafe};

const MAX_STREAM_BYTES: usize = 64 * 1024 * 1024;
const MAX_DOCUMENT_BYTES: usize = 256 * 1024 * 1024;

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
    let pages = guarded(|| extract_checked(bytes))??;
    Ok(render_pages(&pages))
}

/// Parse the document once for integrity checks, then extract text.
///
/// `pdf-extract` uses lopdf's lenient content parser, which silently drops
/// bytes after a corrupt operation. That would ingest a partial page and let
/// reconciliation delete the chunks the partial text no longer contains. This
/// preflight rejects such documents instead, and bounds stream decompression so
/// a small malicious PDF cannot exhaust worker memory.
fn extract_checked(bytes: &[u8]) -> Result<Vec<String>, PdfError> {
    let mut document = pdf_extract::Document::load_mem(bytes)
        .map_err(|error| PdfError::Extract(error.to_string()))?;
    if document.is_encrypted() {
        // Match pdf-extract: only the empty user password is attempted.
        document
            .decrypt("")
            .map_err(|error| PdfError::Extract(error.to_string()))?;
    }
    validate_streams(&document)?;
    for page_id in document.get_pages().values() {
        let content = document
            .get_page_content(*page_id)
            .map_err(|error| PdfError::Extract(error.to_string()))?;
        pdf_extract::content::Content::decode_strict(&content)
            .map_err(|error| PdfError::Extract(format!("corrupt page content: {error}")))?;
    }
    pdf_extract::extract_text_from_mem_by_pages(bytes)
        .map_err(|error| PdfError::Extract(error.to_string()))
}

fn validate_streams(document: &pdf_extract::Document) -> Result<(), PdfError> {
    validate_streams_with_limits(document, MAX_STREAM_BYTES, MAX_DOCUMENT_BYTES)
}

fn validate_streams_with_limits(
    document: &pdf_extract::Document,
    stream_limit: usize,
    document_limit: usize,
) -> Result<(), PdfError> {
    let mut total = 0usize;
    for object in document.objects.values() {
        let pdf_extract::Object::Stream(stream) = object else {
            continue;
        };
        let expanded = match stream.dict.get(b"Filter") {
            // No /Filter key means the stream is stored uncompressed.
            Err(_) => stream.content.len(),
            Ok(_) => {
                let filters = stream.filters().map_err(|error| {
                    PdfError::Extract(format!("invalid stream filter: {error}"))
                })?;
                if filters.is_empty() {
                    stream.content.len()
                } else if filters == [b"FlateDecode".as_slice()] {
                    bounded_inflate_with_limit(&stream.content, stream_limit)?
                } else {
                    // Uncommon filters (LZW/ASCII85/RunLength) can expand, but
                    // their output is bounded in practice; reject absurd
                    // compressed input.
                    if stream.content.len() > stream_limit {
                        return Err(PdfError::Extract(
                            "compressed stream exceeds the ingest size limit".to_string(),
                        ));
                    }
                    stream.content.len()
                }
            }
        };
        if expanded > stream_limit {
            return Err(PdfError::Extract(
                "decompressed stream exceeds the ingest size limit".to_string(),
            ));
        }
        total = total.saturating_add(expanded);
        if total > document_limit {
            return Err(PdfError::Extract(
                "decompressed document exceeds the ingest size limit".to_string(),
            ));
        }
    }
    Ok(())
}

/// Inflate a zlib stream while refusing to allocate past the cap.
fn bounded_inflate_with_limit(compressed: &[u8], limit: usize) -> Result<usize, PdfError> {
    let mut decoder = flate2::read::ZlibDecoder::new(compressed);
    let mut buffer = [0u8; 16 * 1024];
    let mut total = 0usize;
    loop {
        let read = decoder
            .read(&mut buffer)
            .map_err(|error| PdfError::Extract(format!("cannot inflate stream: {error}")))?;
        if read == 0 {
            return Ok(total);
        }
        total = total.saturating_add(read);
        if total > limit {
            return Err(PdfError::Extract(
                "decompressed stream exceeds the ingest size limit".to_string(),
            ));
        }
    }
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

/// Match pdfplumber's line shape: one space between words, no line edges, and
/// no empty lines. pdfplumber rebuilds each line from words and joins them with
/// a single space, so literal runs of spaces and tabs must collapse too.
fn normalize_page(page: &str) -> String {
    page.lines()
        .map(|line| line.split_whitespace().collect::<Vec<_>>().join(" "))
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
        assert_eq!(
            normalize_page(" multiple   spaces\tstay single \n"),
            "multiple spaces stay single"
        );
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
    fn corrupt_page_content_is_rejected_instead_of_partially_ingested() {
        let error = render_pdf(include_bytes!("../fixtures/pdf/broken.pdf"))
            .expect_err("strict page content");
        assert!(
            error.to_string().contains("corrupt page content"),
            "{error}"
        );
    }

    #[test]
    fn decompression_bomb_is_rejected_before_extraction() {
        use std::io::Write;
        let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::best());
        encoder.write_all(&[0u8; 20]).expect("encode");
        let compressed = encoder.finish().expect("finish");
        let error = bounded_inflate_with_limit(&compressed, 10).expect_err("bomb");
        assert!(error.to_string().contains("size limit"), "{error}");
        assert_eq!(
            bounded_inflate_with_limit(&compressed, 20).expect("fits"),
            20
        );
    }

    fn document_with_streams(
        streams: Vec<(pdf_extract::Dictionary, Vec<u8>)>,
    ) -> pdf_extract::Document {
        let mut document = pdf_extract::Document::with_version("1.4");
        for (index, (dictionary, content)) in streams.into_iter().enumerate() {
            document.objects.insert(
                (index as u32 + 1, 0),
                pdf_extract::Object::Stream(pdf_extract::Stream::new(dictionary, content)),
            );
        }
        document
    }

    fn stream_with_filter(
        filter: Option<pdf_extract::Object>,
        content: Vec<u8>,
    ) -> (pdf_extract::Dictionary, Vec<u8>) {
        let mut dictionary = pdf_extract::Dictionary::new();
        if let Some(filter) = filter {
            dictionary.set("Filter", filter);
        }
        (dictionary, content)
    }

    #[test]
    fn invalid_stream_filter_is_rejected() {
        let document = document_with_streams(vec![stream_with_filter(
            Some(pdf_extract::Object::Integer(1)),
            b"data".to_vec(),
        )]);
        let error = validate_streams_with_limits(&document, 100, 100).expect_err("filter");
        assert!(
            error.to_string().contains("invalid stream filter"),
            "{error}"
        );
    }

    #[test]
    fn stream_limits_bound_uncompressed_and_other_filters() {
        let plain = document_with_streams(vec![stream_with_filter(None, b"12345".to_vec())]);
        assert!(validate_streams_with_limits(&plain, 5, 5).is_ok());
        let error = validate_streams_with_limits(&plain, 4, 100).expect_err("stream cap");
        assert!(error.to_string().contains("decompressed stream"), "{error}");
        let error = validate_streams_with_limits(&plain, 100, 4).expect_err("document cap");
        assert!(
            error.to_string().contains("decompressed document"),
            "{error}"
        );

        // Filters other than FlateDecode are bounded by their compressed size.
        let other = document_with_streams(vec![stream_with_filter(
            Some(pdf_extract::Object::Name(b"LZWDecode".to_vec())),
            b"12345".to_vec(),
        )]);
        assert!(validate_streams_with_limits(&other, 5, 5).is_ok());
        let error = validate_streams_with_limits(&other, 4, 100).expect_err("compressed cap");
        assert!(error.to_string().contains("compressed stream"), "{error}");

        // An explicitly empty filter array is stored uncompressed.
        let empty = document_with_streams(vec![stream_with_filter(
            Some(pdf_extract::Object::Array(Vec::new())),
            b"12345".to_vec(),
        )]);
        assert!(validate_streams_with_limits(&empty, 5, 5).is_ok());
    }

    #[test]
    fn panic_boundary_reports_panic() {
        let error = guarded(|| panic!("boom")).expect_err("panic");
        assert!(matches!(error, PdfError::Panic));
    }
}
