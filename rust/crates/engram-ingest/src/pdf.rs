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
use std::fmt::Write as _;
use std::io::Read;
use std::panic::{catch_unwind, AssertUnwindSafe};

use pdf_extract::{
    Dictionary, Document, MediaBox, Object, OutputDev, OutputError, Stream, Transform,
};

const MAX_STREAM_BYTES: usize = 64 * 1024 * 1024;
const MAX_DOCUMENT_BYTES: usize = 256 * 1024 * 1024;
pub(crate) const MAX_INPUT_BYTES: u64 = 256 * 1024 * 1024;
const MAX_PAGES: usize = 10_000;
const MAX_PAGE_CONTENT_STREAMS: usize = 1_024;
const MAX_PAGE_CONTENT_BYTES: usize = 64 * 1024 * 1024;
const MAX_FORM_DEPTH: usize = 64;
const MAX_FORMS: usize = 10_000;

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

/// Per-page plain-text sink for `output_doc`, matching pdf-extract's
/// `PlainTextOutput` spacing/line rules but recording page boundaries in one
/// traversal. A blank page remains an empty entry so page numbering is stable.
struct PageTextOutput {
    pages: Vec<String>,
    current: String,
    flip_ctm: Transform,
    last_end: f64,
    last_y: f64,
    first_char: bool,
}

impl Default for PageTextOutput {
    fn default() -> Self {
        Self {
            pages: Vec::new(),
            current: String::new(),
            flip_ctm: Transform::identity(),
            last_end: 100_000.0,
            last_y: 0.0,
            first_char: false,
        }
    }
}

impl OutputDev for PageTextOutput {
    fn begin_page(
        &mut self,
        _page_num: u32,
        media_box: &MediaBox,
        _art_box: Option<(f64, f64, f64, f64)>,
    ) -> Result<(), OutputError> {
        self.current.clear();
        self.flip_ctm =
            Transform::row_major(1.0, 0.0, 0.0, -1.0, 0.0, media_box.ury - media_box.lly);
        self.last_end = 100_000.0;
        self.last_y = 0.0;
        self.first_char = false;
        Ok(())
    }

    fn end_page(&mut self) -> Result<(), OutputError> {
        self.pages.push(std::mem::take(&mut self.current));
        Ok(())
    }

    fn output_character(
        &mut self,
        trm: &Transform,
        width: f64,
        _spacing: f64,
        font_size: f64,
        character: &str,
    ) -> Result<(), OutputError> {
        let position = trm.post_transform(&self.flip_ctm);
        let size = trm.transform_vector(euclid::vec2(font_size, font_size));
        let size = (size.x * size.y).sqrt();
        let (x, y) = (position.m31, position.m32);
        if self.first_char {
            if (y - self.last_y).abs() > size * 1.5 {
                writeln!(self.current)?;
            }
            if x < self.last_end && (y - self.last_y).abs() > size * 0.5 {
                writeln!(self.current)?;
            }
            if x > self.last_end + size * 0.1 {
                write!(self.current, " ")?;
            }
        }
        write!(self.current, "{character}")?;
        self.first_char = false;
        self.last_y = y;
        self.last_end = x + width * size;
        Ok(())
    }

    fn begin_word(&mut self) -> Result<(), OutputError> {
        self.first_char = true;
        Ok(())
    }

    fn end_word(&mut self) -> Result<(), OutputError> {
        Ok(())
    }

    fn end_line(&mut self) -> Result<(), OutputError> {
        Ok(())
    }
}

/// Best-effort bound on Flate expansion before `Document::load_mem` inflates
/// object/xref streams. lopdf has no load-time budget hook; scanning the raw
/// stream segments with a direct `/Length` catches the classic bomb before the
/// parser allocates it. Failures to decode (encrypted/unsupported streams) are
/// left to the post-load validation.
fn preflight_load_streams(bytes: &[u8]) -> Result<(), PdfError> {
    preflight_load_streams_with_limits(bytes, MAX_STREAM_BYTES, MAX_DOCUMENT_BYTES)
}

fn preflight_load_streams_with_limits(
    bytes: &[u8],
    stream_limit: usize,
    document_limit: usize,
) -> Result<(), PdfError> {
    let mut total = 0usize;
    let mut cursor = 0usize;
    while let Some(offset) = find_bytes(&bytes[cursor..], b"stream") {
        let keyword = cursor + offset;
        cursor = keyword + b"stream".len();
        if !stream_keyword_boundary(bytes, keyword) {
            continue;
        }
        let Some(data_start) = stream_data_start(bytes, cursor) else {
            continue;
        };
        let dict_start = bytes[..keyword]
            .windows(3)
            .rposition(|window| window == b"obj")
            .map(|position| position + 3)
            .unwrap_or(0);
        let dictionary = &bytes[dict_start..keyword];
        if contains_unsupported_load_filter(dictionary) {
            return Err(PdfError::Extract(
                "LZW/RunLength streams are not supported in the load path".to_string(),
            ));
        }
        if !contains_flate_filter(dictionary) {
            continue;
        }
        let Some(data_end) = stream_data_end(bytes, dictionary, data_start) else {
            continue;
        };
        let Some(decoded) = decode_flate_optional(&bytes[data_start..data_end], stream_limit)?
        else {
            continue;
        };
        total = total.saturating_add(decoded.len());
        if total > document_limit {
            return Err(PdfError::Extract(
                "decompressed document exceeds the ingest limit during load".to_string(),
            ));
        }
    }
    Ok(())
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn stream_keyword_boundary(bytes: &[u8], position: usize) -> bool {
    let before = position
        .checked_sub(1)
        .and_then(|index| bytes.get(index))
        .is_none_or(|byte| byte.is_ascii_whitespace() || *byte == b'>');
    let after = bytes.get(position + b"stream".len()).copied();
    before && matches!(after, Some(b'\r' | b'\n'))
}

fn stream_data_start(bytes: &[u8], keyword_end: usize) -> Option<usize> {
    match bytes.get(keyword_end..keyword_end + 2) {
        Some(b"\r\n") => Some(keyword_end + 2),
        _ => match bytes.get(keyword_end) {
            Some(b'\n' | b'\r') => Some(keyword_end + 1),
            _ => None,
        },
    }
}

fn contains_flate_filter(dictionary: &[u8]) -> bool {
    find_bytes(dictionary, b"/FlateDecode").is_some()
}

fn contains_unsupported_load_filter(dictionary: &[u8]) -> bool {
    find_bytes(dictionary, b"/LZWDecode").is_some()
        || find_bytes(dictionary, b"/RunLengthDecode").is_some()
}

fn stream_data_end(bytes: &[u8], dictionary: &[u8], data_start: usize) -> Option<usize> {
    if let Some(length) = direct_stream_length(dictionary) {
        let end = data_start.checked_add(length)?;
        if end <= bytes.len() {
            return Some(end);
        }
    }
    let end = find_bytes(&bytes[data_start..], b"endstream")?;
    let mut end = data_start + end;
    while end > data_start && matches!(bytes[end - 1], b'\r' | b'\n') {
        end -= 1;
    }
    Some(end)
}

fn direct_stream_length(dictionary: &[u8]) -> Option<usize> {
    let position = dictionary
        .windows(7)
        .rposition(|window| window == b"/Length")?;
    let mut index = position + 7;
    while matches!(dictionary.get(index), Some(byte) if byte.is_ascii_whitespace()) {
        index += 1;
    }
    let start = index;
    while matches!(dictionary.get(index), Some(byte) if byte.is_ascii_digit()) {
        index += 1;
    }
    if start == index {
        return None;
    }
    let value = std::str::from_utf8(&dictionary[start..index])
        .ok()?
        .parse()
        .ok()?;
    while matches!(dictionary.get(index), Some(byte) if byte.is_ascii_whitespace()) {
        index += 1;
    }
    // `/Length 12 0 R` is indirect; fall back to the `endstream` boundary.
    if matches!(dictionary.get(index), Some(byte) if byte.is_ascii_digit()) {
        return None;
    }
    Some(value)
}

/// Decode Flate for the load preflight. `None` means the bytes are not a
/// decodable Flate stream (encrypted or another filter); leave them to the
/// post-load validation. A limit hit is always an error.
fn decode_flate_optional(compressed: &[u8], limit: usize) -> Result<Option<Vec<u8>>, PdfError> {
    if compressed.is_empty() {
        return Ok(Some(Vec::new()));
    }
    match inflate_with(flate2::read::ZlibDecoder::new(compressed), limit) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(InflateFailure::Limit) => Err(size_limit_error()),
        Err(InflateFailure::Decode(_)) => {
            match inflate_with(flate2::read::DeflateDecoder::new(compressed), limit) {
                Ok(bytes) => Ok(Some(bytes)),
                Err(InflateFailure::Limit) => Err(size_limit_error()),
                Err(InflateFailure::Decode(_)) => Ok(None),
            }
        }
    }
}

/// Parse once for integrity checks, then let the extractor produce text.
fn extract_checked(bytes: &[u8]) -> Result<Vec<String>, PdfError> {
    preflight_load_streams(bytes)?;
    let mut document =
        Document::load_mem(bytes).map_err(|error| PdfError::Extract(error.to_string()))?;
    validate_loaded_objects(&document)?;
    if document.is_encrypted() {
        // Match pdf-extract: only the empty user password is attempted.
        document
            .decrypt("")
            .map_err(|error| PdfError::Extract(error.to_string()))?;
    }
    validate_stream_integrity(&document)?;
    let tree_pages = validate_page_tree(&document)?;
    let pages = document.get_pages();
    if pages.len() != tree_pages {
        return Err(PdfError::Extract(format!(
            "PDF page tree exposes {} pages but only {} are valid",
            tree_pages,
            pages.len()
        )));
    }
    if pages.len() > MAX_PAGES {
        return Err(PdfError::Extract(format!(
            "PDF has {} pages, above the ingest limit of {MAX_PAGES}",
            pages.len()
        )));
    }
    let mut seen_forms = HashSet::new();
    for page_id in pages.values() {
        validate_page(&document, *page_id, &mut seen_forms)?;
    }
    // Extract from the document that was just validated and decrypted: one
    // page-tree walk, no second parse, and no divergence between the bytes
    // checked and the bytes rendered.
    let mut output = PageTextOutput::default();
    pdf_extract::output_doc(&document, &mut output)
        .map_err(|error| PdfError::Extract(error.to_string()))?;
    Ok(output.pages)
}

/// Validate the page tree before `get_pages()` so an invalid `/Kids` branch
/// cannot be dropped silently while extraction returns the surviving pages.
fn validate_page_tree(document: &Document) -> Result<usize, PdfError> {
    let root = document
        .trailer
        .get(b"Root")
        .map_err(|_| PdfError::Extract("PDF has no /Root".to_string()))?;
    let (_, root) = document
        .dereference(root)
        .map_err(|error| PdfError::Extract(error.to_string()))?;
    let root = root
        .as_dict()
        .map_err(|_| PdfError::Extract("PDF /Root is not a dictionary".to_string()))?;
    let pages = root
        .get(b"Pages")
        .map_err(|_| PdfError::Extract("PDF catalog has no /Pages".to_string()))?;
    let (_, pages) = document
        .dereference(pages)
        .map_err(|error| PdfError::Extract(error.to_string()))?;
    let mut visited = HashSet::new();
    count_page_tree(document, pages, &mut visited, 0)
}

fn count_page_tree(
    document: &Document,
    node: &Object,
    visited: &mut HashSet<pdf_extract::ObjectId>,
    depth: usize,
) -> Result<usize, PdfError> {
    if depth > MAX_FORM_DEPTH {
        return Err(PdfError::Extract(
            "PDF page tree nesting exceeds the ingest limit".to_string(),
        ));
    }
    let node = node
        .as_dict()
        .map_err(|_| PdfError::Extract("PDF page tree node is not a dictionary".to_string()))?;
    match node.get(b"Kids") {
        Ok(kids) => {
            let (_, kids) = document
                .dereference(kids)
                .map_err(|error| PdfError::Extract(error.to_string()))?;
            let kids = kids.as_array().map_err(|_| {
                PdfError::Extract("PDF page tree /Kids is not an array".to_string())
            })?;
            let mut total = 0usize;
            for kid in kids {
                let (id, kid) = document
                    .dereference(kid)
                    .map_err(|error| PdfError::Extract(error.to_string()))?;
                if let Some(id) = id {
                    if !visited.insert(id) {
                        return Err(PdfError::Extract(
                            "PDF page tree contains a cycle".to_string(),
                        ));
                    }
                }
                total += count_page_tree(document, kid, visited, depth + 1)?;
            }
            Ok(total)
        }
        Err(_) => {
            // A leaf without /Kids is a page. A /Pages node without /Kids is
            // invalid and must not be counted as a page.
            let is_pages = node
                .get(b"Type")
                .ok()
                .and_then(|value| document.dereference(value).ok())
                .and_then(|(_, value)| value.as_name().ok())
                == Some(b"Pages".as_slice());
            if is_pages {
                return Err(PdfError::Extract(
                    "PDF /Pages node has no /Kids".to_string(),
                ));
            }
            Ok(1)
        }
    }
}

/// Require every non-free xref entry to have a loaded object. lopdf logs and
/// skips an unreadable object; a missing page/content object would otherwise
/// become silent empty content. Run before decryption because lopdf removes the
/// encryption dictionary and its xref entry after `decrypt`.
fn validate_loaded_objects(document: &Document) -> Result<(), PdfError> {
    let loaded: HashSet<u32> = document.objects.keys().map(|(number, _)| *number).collect();
    let mut missing = Vec::new();
    for (id, entry) in &document.reference_table.entries {
        if !(entry.is_normal() || entry.is_compressed()) {
            continue;
        }
        if !loaded.contains(id) {
            missing.push(*id);
        }
    }
    // lopdf removes the encryption dictionary after decoding it with the empty
    // user password, so exactly that xref entry is expected to be absent.
    if !missing.is_empty() && !(missing.len() == 1 && document.encryption_state.is_some()) {
        return Err(PdfError::Extract(format!(
            "PDF object(s) {missing:?} are referenced but could not be loaded"
        )));
    }
    Ok(())
}

/// Validate stream lengths and bound every stream the extractor may inflate.
/// Run after decryption so the bytes are plaintext.
fn validate_stream_integrity(document: &Document) -> Result<(), PdfError> {
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
    validate_page_with_limit(document, page_id, seen_forms, MAX_PAGE_CONTENT_BYTES)
}

fn validate_page_with_limit(
    document: &Document,
    page_id: pdf_extract::ObjectId,
    seen_forms: &mut HashSet<pdf_extract::ObjectId>,
    page_limit: usize,
) -> Result<(), PdfError> {
    let mut content = Vec::new();
    let mut streams = 0usize;
    for content_id in document.get_page_contents(page_id) {
        streams += 1;
        if streams > MAX_PAGE_CONTENT_STREAMS {
            return Err(PdfError::Extract(
                "page has too many content streams".to_string(),
            ));
        }
        let stream = document
            .get_object(content_id)
            .and_then(Object::as_stream)
            .map_err(|error| {
                PdfError::Extract(format!(
                    "page content {content_id:?} is unreadable: {error}"
                ))
            })?;
        let decoded = decompress_stream(document, stream)?;
        if content
            .len()
            .saturating_add(decoded.len())
            .saturating_add(1)
            > page_limit
        {
            return Err(PdfError::Extract(
                "page content exceeds the ingest size limit".to_string(),
            ));
        }
        content.extend_from_slice(&decoded);
        content.push(b'\n');
    }
    // NUL and form feed are PDF whitespace, but lopdf's extraction parser
    // stops at them. Reject explicitly: accepting them here would let
    // extraction truncate the page and reconciliation delete its chunks.
    if content.iter().any(|byte| matches!(byte, 0 | 0x0C)) {
        return Err(PdfError::Extract(
            "page content uses NUL/form-feed whitespace, which the extractor cannot parse safely"
                .to_string(),
        ));
    }
    pdf_extract::content::Content::decode_strict(&content)
        .map_err(|error| PdfError::Extract(format!("corrupt page content: {error}")))?;

    let page = document
        .get_object(page_id)
        .and_then(Object::as_dict)
        .map_err(|error| PdfError::Extract(error.to_string()))?;
    if let Some(resources) = inherited_resources(document, page, page_id)? {
        validate_resources(document, resources, seen_forms, &mut HashSet::new(), 0)?;
    }
    Ok(())
}

/// Resolve `/Resources` through the page tree like pdf-extract does
/// (`get_inherited`), rejecting a cyclic or over-deep `/Parent` chain so the
/// extractor cannot follow it recursively without a bound.
fn inherited_resources<'a>(
    document: &'a Document,
    page: &'a Dictionary,
    page_id: pdf_extract::ObjectId,
) -> Result<Option<&'a Dictionary>, PdfError> {
    let mut current = page;
    let mut visited = HashSet::from([page_id]);
    for _ in 0..MAX_FORM_DEPTH {
        if let Ok(resources) = current.get(b"Resources") {
            let (_, resources) = document
                .dereference(resources)
                .map_err(|error| PdfError::Extract(error.to_string()))?;
            return resources
                .as_dict()
                .map(Some)
                .map_err(|_| PdfError::Extract("PDF /Resources is not a dictionary".to_string()));
        }
        let parent = match current.get(b"Parent") {
            Ok(parent) => parent,
            Err(_) => return Ok(None),
        };
        let (id, parent) = document
            .dereference(parent)
            .map_err(|error| PdfError::Extract(error.to_string()))?;
        if let Some(id) = id {
            if !visited.insert(id) {
                return Err(PdfError::Extract(
                    "PDF page /Parent chain contains a cycle".to_string(),
                ));
            }
        }
        current = parent
            .as_dict()
            .map_err(|_| PdfError::Extract("PDF /Parent is not a dictionary".to_string()))?;
    }
    Err(PdfError::Extract(
        "PDF page /Parent chain exceeds the ingest limit".to_string(),
    ))
}

/// Strict-decode every Form XObject reachable from a resource dictionary.
/// Image XObjects are skipped: the extractor does not turn them into text.
fn validate_resources(
    document: &Document,
    resources: &Dictionary,
    seen_forms: &mut HashSet<pdf_extract::ObjectId>,
    active_forms: &mut HashSet<pdf_extract::ObjectId>,
    depth: usize,
) -> Result<(), PdfError> {
    if depth > MAX_FORM_DEPTH {
        return Err(PdfError::Extract(
            "Form XObject nesting exceeds the ingest limit".to_string(),
        ));
    }
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
            if active_forms.contains(&id) {
                return Err(PdfError::Extract(
                    "PDF Form XObject invocation contains a cycle".to_string(),
                ));
            }
            if seen_forms.contains(&id) {
                continue;
            }
            active_forms.insert(id);
        }
        if seen_forms.len() > MAX_FORMS {
            return Err(PdfError::Extract(
                "PDF has too many Form XObjects".to_string(),
            ));
        }
        let content = decompress_stream(document, stream)?;
        if content.iter().any(|byte| matches!(byte, 0 | 0x0C)) {
            return Err(PdfError::Extract(
                "Form XObject content uses NUL/form-feed whitespace, which the extractor cannot \
                 parse safely"
                    .to_string(),
            ));
        }
        pdf_extract::content::Content::decode_strict(&content)
            .map_err(|error| PdfError::Extract(format!("corrupt Form XObject content: {error}")))?;
        let nested = stream
            .dict
            .get(b"Resources")
            .ok()
            .and_then(|value| document.dereference(value).ok())
            .and_then(|(_, value)| value.as_dict().ok());
        if let Some(nested) = nested {
            validate_resources(document, nested, seen_forms, active_forms, depth + 1)?;
        }
        if let Some(id) = id {
            active_forms.remove(&id);
            seen_forms.insert(id);
        }
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
        None => {
            if stream.content.len() > limit {
                return Err(PdfError::Extract(
                    "decompressed stream exceeds the ingest size limit".to_string(),
                ));
            }
            Ok(stream.content.clone())
        }
        Some([name]) if name == b"FlateDecode" => decode_flate(&stream.content, limit),
        Some([name]) if name == b"ASCII85Decode" => decode_ascii85(&stream.content, limit),
        Some([first, second]) if first == b"ASCII85Decode" && second == b"FlateDecode" => {
            let intermediate = decode_ascii85(&stream.content, limit)?;
            decode_flate(&intermediate, limit)
        }
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
    // pdf-extract does not dereference an indirect /Filter, so validation and
    // extraction would disagree about the stream bytes. Reject it explicitly.
    if matches!(raw, Object::Reference(_)) {
        return Err(PdfError::Extract(
            "indirect /Filter is not supported".to_string(),
        ));
    }
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

/// Decode PDF ASCII85 (`/ASCII85Decode`), including `z` zero groups and a
/// partial final group. Output is bounded even though ASCII85 alone cannot
/// expand beyond its input.
fn decode_ascii85(input: &[u8], limit: usize) -> Result<Vec<u8>, PdfError> {
    let mut output = Vec::new();
    let mut group = [0u8; 5];
    let mut count = 0usize;
    for &byte in input {
        if byte.is_ascii_whitespace() {
            continue;
        }
        if byte == b'~' {
            break;
        }
        if byte == b'z' {
            if count != 0 {
                return Err(PdfError::Extract(
                    "invalid ASCII85 'z' inside a group".to_string(),
                ));
            }
            if output.len().saturating_add(4) > limit {
                return Err(size_limit_error());
            }
            output.extend_from_slice(&[0, 0, 0, 0]);
            continue;
        }
        if !(b'!'..=b'u').contains(&byte) {
            return Err(PdfError::Extract("invalid ASCII85 character".to_string()));
        }
        group[count] = byte - b'!';
        count += 1;
        if count == 5 {
            if output.len().saturating_add(4) > limit {
                return Err(size_limit_error());
            }
            let mut value = 0u64;
            for &digit in &group {
                value = value * 85 + digit as u64;
            }
            output.extend_from_slice(&(value as u32).to_be_bytes());
            count = 0;
        }
    }
    if count > 0 {
        if count == 1 {
            return Err(PdfError::Extract("invalid ASCII85 final group".to_string()));
        }
        for digit in group.iter_mut().skip(count) {
            *digit = 84;
        }
        let mut value = 0u64;
        for &digit in &group {
            value = value * 85 + digit as u64;
        }
        let bytes = (value as u32).to_be_bytes();
        let take = count - 1;
        if output.len().saturating_add(take) > limit {
            return Err(size_limit_error());
        }
        output.extend_from_slice(&bytes[..take]);
    }
    Ok(output)
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
        .map(|line| {
            line.split_whitespace()
                .map(expand_ligatures)
                .collect::<Vec<_>>()
                .join(" ")
        })
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

/// pdfplumber expands common Latin ligatures; pdf-extract preserves the
/// compatibility codepoint. Apply the same targeted mapping so the rendered
/// hash and retained text match.
fn expand_ligatures(word: &str) -> String {
    let mut expanded = String::with_capacity(word.len());
    for character in word.chars() {
        match character {
            '\u{FB00}' => expanded.push_str("ff"),
            '\u{FB01}' => expanded.push_str("fi"),
            '\u{FB02}' => expanded.push_str("fl"),
            '\u{FB03}' => expanded.push_str("ffi"),
            '\u{FB04}' => expanded.push_str("ffl"),
            '\u{FB05}' => expanded.push_str("ft"),
            '\u{FB06}' => expanded.push_str("st"),
            other => expanded.push(other),
        }
    }
    expanded
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
    fn load_preflight_rejects_flate_bomb_before_parser() {
        use std::io::Write;
        let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::best());
        encoder.write_all(&[0u8; 100]).expect("encode");
        let compressed = encoder.finish().expect("finish");
        let mut pdf = format!(
            "<< /Length {} /Filter /FlateDecode >>\nstream\n",
            compressed.len()
        )
        .into_bytes();
        pdf.extend_from_slice(&compressed);
        pdf.extend_from_slice(b"\nendstream");
        let error = preflight_load_streams_with_limits(&pdf, 10, 100).expect_err("bomb");
        assert!(error.to_string().contains("size limit"), "{error}");
        assert!(preflight_load_streams_with_limits(&pdf, 100, 100).is_ok());
    }

    #[test]
    fn load_preflight_ignores_non_flate_and_undecodable_streams() {
        let plain = b"<< /Length 5 >>\nstream\nhello\nendstream";
        assert!(preflight_load_streams_with_limits(plain, 1, 1).is_ok());
        let encrypted =
            b"<< /Length 4 /Filter /FlateDecode >>\nstream\n\xFF\xFE\xFD\xFC\nendstream";
        assert!(preflight_load_streams_with_limits(encrypted, 1, 1).is_ok());
    }

    #[test]
    fn load_preflight_rejects_unbounded_filter_chains() {
        let lzw = b"<< /Length 5 /Filter /LZWDecode >>\nstream\nhello\nendstream";
        let error = preflight_load_streams_with_limits(lzw, 100, 100).expect_err("lzw");
        assert!(error.to_string().contains("LZW/RunLength"), "{error}");
        let run_length = b"<< /Length 5 /Filter /RunLengthDecode >>\nstream\nhello\nendstream";
        assert!(preflight_load_streams_with_limits(run_length, 100, 100).is_err());
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
        assert!(validate_stream_integrity(&missing)
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
        assert!(validate_stream_integrity(&mismatch)
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
        assert!(validate_stream_integrity(&non_integer)
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
        assert!(validate_stream_integrity(&indirect).is_ok());
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
        assert!(validate_loaded_objects(&document)
            .unwrap_err()
            .to_string()
            .contains("could not be loaded"));

        let mut free = Document::new();
        free.reference_table
            .insert(7, pdf_extract::xref::XrefEntry::Free);
        assert!(validate_loaded_objects(&free).is_ok());
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
        assert!(validate_all_streams_with_limits(&indirect_filter, 100, 100)
            .unwrap_err()
            .to_string()
            .contains("indirect /Filter"));
    }

    #[test]
    fn ascii85_and_ascii85_flate_chains_decode_in_order() {
        let document = Document::new();
        let mut single = Dictionary::new();
        single.set("Filter", Object::Name(b"ASCII85Decode".to_vec()));
        let single = Stream::new(single, b"87cURDZ~>".to_vec());
        assert_eq!(decode_stream(&document, &single, 100).unwrap(), b"Hello");

        let mut chain = Dictionary::new();
        chain.set(
            "Filter",
            Object::Array(vec![
                Object::Name(b"ASCII85Decode".to_vec()),
                Object::Name(b"FlateDecode".to_vec()),
            ]),
        );
        let chain = Stream::new(chain, b"Garg^;:'MC<%p.,#Y@tAnAs*Y#g#t`%06Gm5Qu3~>".to_vec());
        assert_eq!(
            decode_stream(&document, &chain, 100).unwrap(),
            b"BT /F1 12 Tf (Hi) Tj ET"
        );
    }

    #[test]
    fn ligatures_expand_like_pdfplumber() {
        assert_eq!(
            normalize_page("A \u{FB01}le and a \u{FB02}ow"),
            "A file and a flow"
        );
        assert_eq!(normalize_page("\u{FB03} \u{FB04} \u{FB00}"), "ffi ffl ff");
    }

    #[test]
    fn cyclic_parent_chain_is_rejected() {
        let mut first = Dictionary::new();
        first.set("Parent", Object::Reference((2, 0)));
        let mut second = Dictionary::new();
        second.set("Parent", Object::Reference((1, 0)));
        let document = document_with(vec![
            ((1, 0), Object::Dictionary(first)),
            ((2, 0), Object::Dictionary(second)),
        ]);
        let mut seen = HashSet::new();
        let error = validate_page(&document, (1, 0), &mut seen).expect_err("cycle");
        assert!(error.to_string().contains("cycle"), "{error}");
    }

    #[test]
    fn cyclic_form_invocation_is_rejected() {
        let mut resources = Dictionary::new();
        let mut xobjects = Dictionary::new();
        xobjects.set("Fm", Object::Reference((3, 0)));
        resources.set("XObject", Object::Dictionary(xobjects));
        let mut page = Dictionary::new();
        page.set("Contents", Object::Reference((2, 0)));
        page.set("Resources", Object::Dictionary(resources));
        let mut form = Dictionary::new();
        form.set("Length", Object::Integer(5));
        form.set("Subtype", Object::Name(b"Form".to_vec()));
        let mut form_resources = Dictionary::new();
        let mut form_xobjects = Dictionary::new();
        form_xobjects.set("Fm", Object::Reference((3, 0)));
        form_resources.set("XObject", Object::Dictionary(form_xobjects));
        form.set("Resources", Object::Dictionary(form_resources));
        let document = document_with(vec![
            ((1, 0), Object::Dictionary(page)),
            ((2, 0), stream_object(b"/Fm Do", None)),
            (
                (3, 0),
                Object::Stream(Stream::new(form, b"/Fm Do".to_vec())),
            ),
        ]);
        let mut seen = HashSet::new();
        let error = validate_page(&document, (1, 0), &mut seen).expect_err("form cycle");
        assert!(error.to_string().contains("cycle"), "{error}");
    }

    #[test]
    fn page_tree_with_non_array_kids_is_rejected() {
        let mut catalog = Dictionary::new();
        catalog.set("Pages", Object::Reference((2, 0)));
        let mut pages = Dictionary::new();
        pages.set("Type", Object::Name(b"Pages".to_vec()));
        pages.set("Kids", Object::Integer(1));
        let document = document_with(vec![
            ((1, 0), Object::Dictionary(catalog)),
            ((2, 0), Object::Dictionary(pages)),
        ]);
        let mut document = document;
        document.trailer.set("Root", Object::Reference((1, 0)));
        let error = validate_page_tree(&document).expect_err("kids");
        assert!(
            error.to_string().contains("/Kids is not an array"),
            "{error}"
        );
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
    fn pdf_nul_and_form_feed_whitespace_is_rejected_not_truncated() {
        let mut document = page_document(Object::Reference((2, 0)), None);
        document
            .objects
            .insert((2, 0), stream_object(b"BT\0/F1 12 Tf (x) Tj ET\x0c", None));
        let mut seen = HashSet::new();
        let error = validate_page(&document, (1, 0), &mut seen).expect_err("unsafe whitespace");
        assert!(error.to_string().contains("NUL/form-feed"), "{error}");
    }

    #[test]
    fn page_content_budget_is_enforced() {
        let document = page_document(Object::Reference((2, 0)), None);
        let mut document = document;
        document
            .objects
            .insert((2, 0), stream_object(b"12345", None));
        let mut seen = HashSet::new();
        let error =
            validate_page_with_limit(&document, (1, 0), &mut seen, 4).expect_err("page budget");
        assert!(
            error.to_string().contains("page content exceeds"),
            "{error}"
        );
    }

    #[test]
    fn deeply_nested_form_xobjects_are_rejected_without_stack_overflow() {
        let mut resources = Dictionary::new();
        let mut xobjects = Dictionary::new();
        xobjects.set("Fm", Object::Reference((10, 0)));
        resources.set("XObject", Object::Dictionary(xobjects));
        let mut page = Dictionary::new();
        page.set("Contents", Object::Reference((2, 0)));
        page.set("Resources", Object::Dictionary(resources));
        let mut objects = vec![
            ((1, 0), Object::Dictionary(page)),
            ((2, 0), stream_object(b"/Fm Do", None)),
        ];
        for index in 0..(MAX_FORM_DEPTH + 2) {
            let mut form = Dictionary::new();
            form.set("Length", Object::Integer(5));
            form.set("Subtype", Object::Name(b"Form".to_vec()));
            if index + 1 < MAX_FORM_DEPTH + 2 {
                let mut resources = Dictionary::new();
                let mut xobjects = Dictionary::new();
                xobjects.set("Fm", Object::Reference((10 + index as u32 + 1, 0)));
                resources.set("XObject", Object::Dictionary(xobjects));
                form.set("Resources", Object::Dictionary(resources));
            }
            objects.push((
                (10 + index as u32, 0),
                Object::Stream(Stream::new(form, b"/Fm Do".to_vec())),
            ));
        }
        let document = document_with(objects);
        let mut seen = HashSet::new();
        let error = validate_page(&document, (1, 0), &mut seen).expect_err("nesting");
        assert!(error.to_string().contains("nesting"), "{error}");
    }

    #[test]
    fn inherited_page_resources_are_strict_validated() {
        let mut xobjects = Dictionary::new();
        xobjects.set("Fm1", Object::Reference((4, 0)));
        let mut resources = Dictionary::new();
        resources.set("XObject", Object::Dictionary(xobjects));

        let mut pages = Dictionary::new();
        pages.set("Type", Object::Name(b"Pages".to_vec()));
        pages.set("Resources", Object::Dictionary(resources));

        let mut page = Dictionary::new();
        page.set("Type", Object::Name(b"Page".to_vec()));
        page.set("Contents", Object::Reference((2, 0)));
        page.set("Parent", Object::Reference((3, 0)));

        let mut form = Dictionary::new();
        form.set("Length", Object::Integer(5));
        form.set("Subtype", Object::Name(b"Form".to_vec()));

        let document = document_with(vec![
            ((1, 0), Object::Dictionary(page)),
            ((2, 0), stream_object(b"/Fm1 Do", None)),
            ((3, 0), Object::Dictionary(pages)),
            ((4, 0), Object::Stream(Stream::new(form, b"BT (x".to_vec()))),
        ]);
        let mut seen = HashSet::new();
        assert!(validate_page(&document, (1, 0), &mut seen)
            .unwrap_err()
            .to_string()
            .contains("Form XObject"));
    }
}
