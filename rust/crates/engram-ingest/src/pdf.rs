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
//! pdfplumber's `extract_text` emits one entry per visual line, collapses
//! whitespace runs, strips line edges, and never emits empty lines.
//! `pdf-extract` keeps literal space runs and paragraph gaps, so
//! [`normalize_page`] applies the same line shape before the page is rendered.
//! The parity fixture in `fixtures/pdf` pins the resulting content
//! byte-for-byte against the Python oracle.
//!
//! The extractor's own content decoding is lenient: lopdf silently truncates a
//! corrupt content stream and drops unreadable indirect objects. Ingesting that
//! partial text would let reconciliation delete the chunks the partial text no
//! longer contains. [`extract_checked`] therefore parses the document first,
//! requires every referenced object and stream length to resolve, strict-decodes
//! page and Form XObject content, and bounds stream decompression. Only then
//! does `pdf-extract` produce text.
//!
//! Supported stream filters are unencoded and `FlateDecode` (the common case).
//! Other filter chains are rejected explicitly instead of being decompressed
//! without a memory bound. Scanned/image-only PDFs produce no text in both
//! implementations; OCR is out of scope (issue #258).

use std::collections::HashSet;
use std::io::Read;
use std::panic::{catch_unwind, AssertUnwindSafe};

use pdf_extract::{Dictionary, Document, Object, Stream};

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

/// Parse once for integrity checks, then let the extractor produce text.
fn extract_checked(bytes: &[u8]) -> Result<Vec<String>, PdfError> {
    let mut document =
        Document::load_mem(bytes).map_err(|error| PdfError::Extract(error.to_string()))?;
    if document.is_encrypted() {
        // Match pdf-extract: only the empty user password is attempted.
        document
            .decrypt("")
            .map_err(|error| PdfError::Extract(error.to_string()))?;
    }
    validate_object_table(&document)?;
    let pages = document.get_pages();
    let mut seen_forms = HashSet::new();
    for page_id in pages.values() {
        validate_page(&document, *page_id, &mut seen_forms)?;
    }
    // Do not hold two parsed documents at once.
    drop(document);
    pdf_extract::extract_text_from_mem_by_pages(bytes)
        .map_err(|error| PdfError::Extract(error.to_string()))
}

/// Require every non-free xref entry to have a loaded object. lopdf logs and
/// skips an unreadable object; a missing page/content object would otherwise
/// become silent empty content.
fn validate_object_table(document: &Document) -> Result<(), PdfError> {
    for (id, entry) in &document.reference_table.entries {
        if !(entry.is_normal() || entry.is_compressed()) {
            continue;
        }
        if !document.objects.keys().any(|(number, _)| number == id) {
            return Err(PdfError::Extract(format!(
                "PDF object {id} is referenced but could not be loaded"
            )));
        }
    }
    for object in document.objects.values() {
        if let Object::Stream(stream) = object {
            validate_stream_length(document, stream)?;
        }
    }
    validate_all_streams(document)
}

/// Bound every stream the extractor may inflate, including image/font streams
/// it walks through XObjects. DCT/JPX image data is already compressed and is
/// not inflated by the extractor, so it is exempt from the expansion cap.
fn validate_all_streams(document: &Document) -> Result<(), PdfError> {
    validate_all_streams_with_limits(document, MAX_STREAM_BYTES, MAX_DOCUMENT_BYTES)
}

fn validate_all_streams_with_limits(
    document: &Document,
    stream_limit: usize,
    document_limit: usize,
) -> Result<(), PdfError> {
    let mut total = 0usize;
    for object in document.objects.values() {
        let Object::Stream(stream) = object else {
            continue;
        };
        let filters = stream_filters(document, stream)?;
        let is_image_codec = stream_subtype(document, stream).as_deref()
            == Some(b"Image".as_slice())
            && matches!(
                filters.as_deref(),
                Some([name]) if matches!(name.as_slice(), b"DCTDecode" | b"JPXDecode")
            );
        if is_image_codec {
            continue;
        }
        let decoded = decode_stream(document, stream, stream_limit)?;
        if decoded.len() > stream_limit {
            return Err(PdfError::Extract(
                "decompressed stream exceeds the ingest size limit".to_string(),
            ));
        }
        total = total.saturating_add(decoded.len());
        if total > document_limit {
            return Err(PdfError::Extract(
                "decompressed document exceeds the ingest size limit".to_string(),
            ));
        }
    }
    Ok(())
}

fn validate_stream_length(document: &Document, stream: &Stream) -> Result<(), PdfError> {
    let raw = stream
        .dict
        .get(b"Length")
        .map_err(|_| PdfError::Extract("stream has no /Length".to_string()))?;
    let (_, length) = document
        .dereference(raw)
        .map_err(|error| PdfError::Extract(error.to_string()))?;
    let length = length
        .as_i64()
        .map_err(|_| PdfError::Extract("stream /Length is not an integer".to_string()))?;
    if length < 0 || length as usize != stream.content.len() {
        return Err(PdfError::Extract(
            "stream /Length does not match the loaded content".to_string(),
        ));
    }
    Ok(())
}

fn validate_page(
    document: &Document,
    page_id: pdf_extract::ObjectId,
    seen_forms: &mut HashSet<pdf_extract::ObjectId>,
) -> Result<(), PdfError> {
    let mut content = Vec::new();
    for content_id in document.get_page_contents(page_id) {
        let stream = document
            .get_object(content_id)
            .and_then(Object::as_stream)
            .map_err(|error| {
                PdfError::Extract(format!(
                    "page content {content_id:?} is unreadable: {error}"
                ))
            })?;
        content.extend_from_slice(&decompress_stream(document, stream)?);
        content.push(b'\n');
    }
    // PDF whitespace includes NUL and form feed, which lopdf's strict content
    // parser does not accept. Normalize only the validation copy.
    let sanitized: Vec<u8> = content
        .iter()
        .map(|byte| {
            if matches!(byte, 0 | 0x0C) {
                b' '
            } else {
                *byte
            }
        })
        .collect();
    pdf_extract::content::Content::decode_strict(&sanitized)
        .map_err(|error| PdfError::Extract(format!("corrupt page content: {error}")))?;

    let page = document
        .get_object(page_id)
        .and_then(Object::as_dict)
        .map_err(|error| PdfError::Extract(error.to_string()))?;
    if let Ok(resources) = page.get(b"Resources") {
        let (_, resources) = document
            .dereference(resources)
            .map_err(|error| PdfError::Extract(error.to_string()))?;
        if let Ok(resources) = resources.as_dict() {
            validate_resources(document, resources, seen_forms)?;
        }
    }
    Ok(())
}

/// Strict-decode every Form XObject reachable from a resource dictionary.
/// Image XObjects are skipped: the extractor does not turn them into text.
fn validate_resources(
    document: &Document,
    resources: &Dictionary,
    seen_forms: &mut HashSet<pdf_extract::ObjectId>,
) -> Result<(), PdfError> {
    let Ok(raw) = resources.get(b"XObject") else {
        return Ok(());
    };
    let (_, xobjects) = document
        .dereference(raw)
        .map_err(|error| PdfError::Extract(error.to_string()))?;
    let Ok(xobjects) = xobjects.as_dict() else {
        return Ok(());
    };
    for (_, object) in xobjects.iter() {
        let (id, object) = document
            .dereference(object)
            .map_err(|error| PdfError::Extract(error.to_string()))?;
        let Object::Stream(stream) = object else {
            continue;
        };
        if stream_subtype(document, stream).as_deref() != Some(b"Form".as_slice()) {
            continue;
        }
        if let Some(id) = id {
            if !seen_forms.insert(id) {
                continue;
            }
        }
        let content = decompress_stream(document, stream)?;
        let sanitized: Vec<u8> = content
            .iter()
            .map(|byte| {
                if matches!(byte, 0 | 0x0C) {
                    b' '
                } else {
                    *byte
                }
            })
            .collect();
        pdf_extract::content::Content::decode_strict(&sanitized)
            .map_err(|error| PdfError::Extract(format!("corrupt Form XObject content: {error}")))?;
        let nested = stream
            .dict
            .get(b"Resources")
            .ok()
            .and_then(|value| document.dereference(value).ok())
            .and_then(|(_, value)| value.as_dict().ok())
            .unwrap_or(resources);
        validate_resources(document, nested, seen_forms)?;
    }
    Ok(())
}

fn stream_subtype(document: &Document, stream: &Stream) -> Option<Vec<u8>> {
    stream
        .dict
        .get(b"Subtype")
        .ok()
        .and_then(|value| document.dereference(value).ok())
        .and_then(|(_, value)| value.as_name().ok())
        .map(<[u8]>::to_vec)
}

/// Return the decoded bytes of a supported stream, with a hard expansion cap.
fn decompress_stream(document: &Document, stream: &Stream) -> Result<Vec<u8>, PdfError> {
    decode_stream(document, stream, MAX_STREAM_BYTES)
}

fn decode_stream(document: &Document, stream: &Stream, limit: usize) -> Result<Vec<u8>, PdfError> {
    let filters = stream_filters(document, stream)?;
    match filters.as_deref() {
        None => Ok(stream.content.clone()),
        Some([name]) if name == b"FlateDecode" => decode_flate(&stream.content, limit),
        Some([name]) if matches!(name.as_slice(), b"DCTDecode" | b"JPXDecode") => Err(
            PdfError::Extract("image stream used as page or Form content".to_string()),
        ),
        Some([]) => Err(PdfError::Extract(
            "empty /Filter array is not a supported stream encoding".to_string(),
        )),
        Some(_) => Err(PdfError::Extract(
            "unsupported stream filter chain; only FlateDecode is decoded".to_string(),
        )),
    }
}

fn stream_filters(document: &Document, stream: &Stream) -> Result<Option<Vec<Vec<u8>>>, PdfError> {
    let Ok(raw) = stream.dict.get(b"Filter") else {
        return Ok(None);
    };
    let (_, value) = document
        .dereference(raw)
        .map_err(|error| PdfError::Extract(format!("invalid stream filter: {error}")))?;
    let mut filters = Vec::new();
    match value {
        Object::Name(name) => filters.push(name.clone()),
        Object::Array(items) => {
            for item in items {
                let (_, item) = document.dereference(item).map_err(|error| {
                    PdfError::Extract(format!("invalid stream filter: {error}"))
                })?;
                let name = item.as_name().map_err(|_| {
                    PdfError::Extract("stream filter array contains a non-name".to_string())
                })?;
                filters.push(name.to_vec());
            }
        }
        other => {
            return Err(PdfError::Extract(format!(
                "invalid stream filter type: {other:?}"
            )))
        }
    }
    Ok(Some(filters))
}

/// Decode FlateDecode with a bounded output buffer. Falls back to raw deflate
/// like lopdf does for streams with a damaged zlib checksum; an empty stream is
/// valid and decodes to nothing.
fn decode_flate(compressed: &[u8], limit: usize) -> Result<Vec<u8>, PdfError> {
    if compressed.is_empty() {
        return Ok(Vec::new());
    }
    match inflate_with(flate2::read::ZlibDecoder::new(compressed), limit) {
        Ok(bytes) => Ok(bytes),
        Err(InflateFailure::Limit) => Err(size_limit_error()),
        Err(InflateFailure::Decode(_)) => {
            match inflate_with(flate2::read::DeflateDecoder::new(compressed), limit) {
                Ok(bytes) => Ok(bytes),
                Err(InflateFailure::Limit) => Err(size_limit_error()),
                Err(InflateFailure::Decode(error)) => {
                    Err(PdfError::Extract(format!("cannot inflate stream: {error}")))
                }
            }
        }
    }
}

fn size_limit_error() -> PdfError {
    PdfError::Extract("decompressed stream exceeds the ingest size limit".to_string())
}

enum InflateFailure {
    Limit,
    Decode(std::io::Error),
}

fn inflate_with(mut decoder: impl Read, limit: usize) -> Result<Vec<u8>, InflateFailure> {
    let mut output = Vec::new();
    let mut buffer = [0u8; 16 * 1024];
    loop {
        let read = decoder.read(&mut buffer).map_err(InflateFailure::Decode)?;
        if read == 0 {
            return Ok(output);
        }
        if output.len().saturating_add(read) > limit {
            return Err(InflateFailure::Limit);
        }
        output.extend_from_slice(&buffer[..read]);
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
    const BLANK: &[u8] = include_bytes!("../fixtures/pdf/blank.pdf");
    const BROKEN: &[u8] = include_bytes!("../fixtures/pdf/broken.pdf");

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
        assert!(rendered.contains("Multiple spaces stay single."));
    }

    #[test]
    fn blank_pdf_has_no_rendered_content() {
        assert_eq!(render_pdf(BLANK).expect("blank"), None);
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
        let error = render_pdf(BROKEN).expect_err("strict page content");
        assert!(
            error.to_string().contains("corrupt page content")
                || error.to_string().contains("Length"),
            "{error}"
        );
    }

    #[test]
    fn decompression_bomb_is_rejected_before_extraction() {
        use std::io::Write;
        let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::best());
        encoder.write_all(&[0u8; 20]).expect("encode");
        let compressed = encoder.finish().expect("finish");
        let error = decode_flate(&compressed, 10).expect_err("bomb");
        assert!(error.to_string().contains("size limit"), "{error}");
        assert_eq!(decode_flate(&compressed, 20).expect("fits").len(), 20);
    }

    #[test]
    fn flate_checksum_damage_falls_back_to_raw_deflate() {
        use std::io::Write;
        let mut encoder =
            flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::best());
        encoder.write_all(b"content stream").expect("encode");
        let raw = encoder.finish().expect("finish");
        assert_eq!(
            decode_flate(&raw, 100).expect("fallback"),
            b"content stream"
        );
    }

    #[test]
    fn panic_boundary_reports_panic() {
        let error = guarded(|| panic!("boom")).expect_err("panic");
        assert!(matches!(error, PdfError::Panic));
    }

    fn stream_object(content: &[u8], filter: Option<Object>) -> Object {
        let mut dictionary = Dictionary::new();
        dictionary.set("Length", Object::Integer(content.len() as i64));
        if let Some(filter) = filter {
            dictionary.set("Filter", filter);
        }
        Object::Stream(Stream::new(dictionary, content.to_vec()))
    }

    fn document_with(objects: Vec<((u32, u16), Object)>) -> Document {
        let mut document = Document::new();
        for (id, object) in objects {
            document.objects.insert(id, object);
        }
        document
    }

    fn page_document(contents: Object, resources: Option<Object>) -> Document {
        let mut page = Dictionary::new();
        page.set("Type", Object::Name(b"Page".to_vec()));
        page.set("Contents", contents);
        if let Some(resources) = resources {
            page.set("Resources", resources);
        }
        document_with(vec![((1, 0), Object::Dictionary(page))])
    }

    #[test]
    fn stream_length_must_be_present_integral_and_matching() {
        let missing = document_with(vec![(
            (1, 0),
            Object::Stream({
                let mut dictionary = Dictionary::new();
                dictionary.set("Length", Object::Integer(3));
                let mut stream = Stream::new(dictionary, b"abc".to_vec());
                stream.dict.remove(b"Length");
                stream
            }),
        )]);
        assert!(validate_object_table(&missing)
            .unwrap_err()
            .to_string()
            .contains("no /Length"));

        let mismatch = document_with(vec![(
            (1, 0),
            Object::Stream({
                let mut stream = Stream::new(Dictionary::new(), b"abc".to_vec());
                stream.dict.set("Length", Object::Integer(9));
                stream
            }),
        )]);
        assert!(validate_object_table(&mismatch)
            .unwrap_err()
            .to_string()
            .contains("does not match"));

        let non_integer = document_with(vec![(
            (1, 0),
            Object::Stream({
                let mut stream = Stream::new(Dictionary::new(), b"abc".to_vec());
                stream.dict.set("Length", Object::Name(b"three".to_vec()));
                stream
            }),
        )]);
        assert!(validate_object_table(&non_integer)
            .unwrap_err()
            .to_string()
            .contains("not an integer"));

        let indirect = document_with(vec![
            (
                (1, 0),
                Object::Stream({
                    let mut stream = Stream::new(Dictionary::new(), b"abc".to_vec());
                    stream.dict.set("Length", Object::Reference((2, 0)));
                    stream
                }),
            ),
            ((2, 0), Object::Integer(3)),
        ]);
        assert!(validate_object_table(&indirect).is_ok());
    }

    #[test]
    fn object_table_rejects_unloaded_xref_entries() {
        let mut document = Document::new();
        document.reference_table.insert(
            7,
            pdf_extract::xref::XrefEntry::Normal {
                offset: 0,
                generation: 0,
            },
        );
        assert!(validate_object_table(&document)
            .unwrap_err()
            .to_string()
            .contains("could not be loaded"));

        let mut free = Document::new();
        free.reference_table
            .insert(7, pdf_extract::xref::XrefEntry::Free);
        assert!(validate_object_table(&free).is_ok());
    }

    #[test]
    fn unsupported_filter_chains_are_rejected_and_image_codecs_are_exempt() {
        let chain = document_with(vec![(
            (1, 0),
            stream_object(
                b"",
                Some(Object::Array(vec![
                    Object::Name(b"FlateDecode".to_vec()),
                    Object::Name(b"FlateDecode".to_vec()),
                ])),
            ),
        )]);
        assert!(validate_all_streams_with_limits(&chain, 100, 100)
            .unwrap_err()
            .to_string()
            .contains("unsupported stream filter chain"));

        let empty = document_with(vec![(
            (1, 0),
            stream_object(b"", Some(Object::Array(Vec::new()))),
        )]);
        assert!(validate_all_streams_with_limits(&empty, 100, 100)
            .unwrap_err()
            .to_string()
            .contains("empty /Filter array"));

        let invalid = document_with(vec![((1, 0), stream_object(b"", Some(Object::Integer(1))))]);
        assert!(validate_all_streams_with_limits(&invalid, 100, 100)
            .unwrap_err()
            .to_string()
            .contains("invalid stream filter type"));

        let mut image = Dictionary::new();
        image.set("Length", Object::Integer(100));
        image.set("Subtype", Object::Name(b"Image".to_vec()));
        image.set("Filter", Object::Name(b"DCTDecode".to_vec()));
        let image = document_with(vec![(
            (1, 0),
            Object::Stream(Stream::new(image, vec![0u8; 100])),
        )]);
        assert!(validate_all_streams_with_limits(&image, 4, 4).is_ok());
        let page_image = document_with(vec![(
            (1, 0),
            stream_object(b"", Some(Object::Name(b"DCTDecode".to_vec()))),
        )]);
        assert!(validate_all_streams_with_limits(&page_image, 100, 100)
            .unwrap_err()
            .to_string()
            .contains("image stream"));

        let mut indirect_filter = Dictionary::new();
        indirect_filter.set("Length", Object::Integer(0));
        indirect_filter.set("Filter", Object::Reference((2, 0)));
        let indirect_filter = document_with(vec![
            (
                (1, 0),
                Object::Stream(Stream::new(indirect_filter, Vec::new())),
            ),
            ((2, 0), Object::Name(b"FlateDecode".to_vec())),
        ]);
        assert!(validate_all_streams_with_limits(&indirect_filter, 100, 100).is_ok());
    }

    #[test]
    fn stream_and_document_expansion_caps_are_enforced() {
        let one = document_with(vec![((1, 0), stream_object(b"12345", None))]);
        assert!(validate_all_streams_with_limits(&one, 5, 5).is_ok());
        assert!(validate_all_streams_with_limits(&one, 4, 100)
            .unwrap_err()
            .to_string()
            .contains("decompressed stream"));
        assert!(validate_all_streams_with_limits(&one, 100, 4)
            .unwrap_err()
            .to_string()
            .contains("decompressed document"));

        let two = document_with(vec![
            ((1, 0), stream_object(b"123", None)),
            ((2, 0), stream_object(b"456", None)),
        ]);
        assert!(validate_all_streams_with_limits(&two, 10, 5)
            .unwrap_err()
            .to_string()
            .contains("decompressed document"));
    }

    #[test]
    fn page_content_references_must_resolve() {
        let document = page_document(Object::Reference((9, 0)), None);
        let mut seen = HashSet::new();
        assert!(validate_page(&document, (1, 0), &mut seen)
            .unwrap_err()
            .to_string()
            .contains("unreadable"));
    }

    #[test]
    fn form_xobject_content_is_strictly_validated() {
        let mut xobjects = Dictionary::new();
        xobjects.set("Fm1", Object::Reference((3, 0)));
        let mut resources = Dictionary::new();
        resources.set("XObject", Object::Dictionary(xobjects));

        let mut form = Dictionary::new();
        form.set("Length", Object::Integer(5));
        form.set("Subtype", Object::Name(b"Form".to_vec()));
        let document = document_with(vec![
            (
                (1, 0),
                Object::Dictionary({
                    let mut page = Dictionary::new();
                    page.set("Contents", Object::Reference((2, 0)));
                    page.set("Resources", Object::Dictionary(resources));
                    page
                }),
            ),
            ((2, 0), stream_object(b"/Fm1 Do", None)),
            ((3, 0), Object::Stream(Stream::new(form, b"BT ET".to_vec()))),
        ]);
        let mut seen = HashSet::new();
        assert!(validate_page(&document, (1, 0), &mut seen).is_ok());

        let mut corrupt_form = Dictionary::new();
        corrupt_form.set("Length", Object::Integer(5));
        corrupt_form.set("Subtype", Object::Name(b"Form".to_vec()));
        let mut xobjects = Dictionary::new();
        xobjects.set("Fm1", Object::Reference((3, 0)));
        let mut resources = Dictionary::new();
        resources.set("XObject", Object::Dictionary(xobjects));
        let corrupt = document_with(vec![
            (
                (1, 0),
                Object::Dictionary({
                    let mut page = Dictionary::new();
                    page.set("Contents", Object::Reference((2, 0)));
                    page.set("Resources", Object::Dictionary(resources));
                    page
                }),
            ),
            ((2, 0), stream_object(b"/Fm1 Do", None)),
            (
                (3, 0),
                Object::Stream(Stream::new(corrupt_form, b"BT (x".to_vec())),
            ),
        ]);
        let mut seen = HashSet::new();
        assert!(validate_page(&corrupt, (1, 0), &mut seen)
            .unwrap_err()
            .to_string()
            .contains("Form XObject"));
    }

    #[test]
    fn pdf_nul_and_form_feed_whitespace_is_accepted() {
        let document = page_document(Object::Reference((2, 0)), None);
        // Rebuild with a content stream containing PDF whitespace outside the
        // strict parser's set.
        let mut document = document;
        document
            .objects
            .insert((2, 0), stream_object(b"BT\0/F1 12 Tf (x) Tj ET\x0c", None));
        let mut seen = HashSet::new();
        validate_page(&document, (1, 0), &mut seen).expect("PDF whitespace is valid");
    }
}
