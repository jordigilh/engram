//! Assembly of the docs retain calls for one source file.
//!
//! This is the Rust equivalent of `_retain_document` in
//! `src/engram/flows/configured.py`: one synthesis for the whole file, one
//! retain call per Markdown section, with the document-level metadata copied to
//! every call.

use std::collections::BTreeMap;
use std::path::Path;

use chrono::{DateTime, FixedOffset, TimeZone, Utc};

use crate::chunking::split_markdown_sections;
use crate::hashing::ContentHash;
use crate::ids::{chunk_document_id, document_base_id};
use crate::python_text;
use crate::synthesis::{synthesize_document, SynthesisError};

/// Compatibility value required by the deprecation contract until a bank
/// reindex changes the metadata contract.
pub const SOURCE_METADATA_VALUE: &str = "cocoindex";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetainCall {
    pub document_id: String,
    pub chunk_key: String,
    pub content: String,
    pub content_sha256: ContentHash,
    pub metadata: BTreeMap<String, String>,
    pub tags: Vec<String>,
    pub timestamp: Option<String>,
}

/// Build every retain call for a Markdown document, or an empty vector when the
/// file has no non-whitespace content (Python `process_doc_file` returns early).
pub fn plan_document(
    source_tag: &str,
    relative_path: &str,
    content: &str,
    timestamp: Option<String>,
) -> Result<Vec<RetainCall>, SynthesisError> {
    if python_text::strip(content).is_empty() {
        return Ok(Vec::new());
    }

    let base_id = document_base_id(source_tag, relative_path);
    let synthesis = synthesize_document(&base_id, content)?;
    let mut metadata = BTreeMap::new();
    metadata.insert("source".to_string(), SOURCE_METADATA_VALUE.to_string());
    metadata.insert("repo".to_string(), source_tag.to_string());
    metadata.insert(
        "key_sentences".to_string(),
        synthesis.key_sentences.join(" | "),
    );
    metadata.insert("keywords".to_string(), synthesis.keywords.join(", "));
    let section = section_tag(relative_path);

    Ok(split_markdown_sections(content, 800, 200)
        .into_iter()
        .map(|(key, chunk)| RetainCall {
            document_id: chunk_document_id(&base_id, &key),
            chunk_key: key,
            content_sha256: ContentHash::of(chunk.as_bytes()),
            content: chunk,
            metadata: metadata.clone(),
            tags: vec![section.clone(), source_tag.to_string()],
            timestamp: timestamp.clone(),
        })
        .collect())
}

/// Python `PurePosixPath(relative).parts[0] if "/" in relative else "root"`.
fn section_tag(relative_path: &str) -> String {
    if !relative_path.contains('/') {
        return "root".to_string();
    }
    if relative_path.starts_with('/') {
        return "/".to_string();
    }
    let normalized = relative_path.trim_start_matches("./");
    normalized
        .split('/')
        .find(|part| !part.is_empty())
        .unwrap_or("root")
        .to_string()
}

/// `_relative_path`: resolve both paths and strip the root; otherwise return the
/// path as given (the Python fallback).
pub fn relative_path(root: &Path, file: &Path) -> String {
    if let (Ok(root), Ok(file)) = (std::fs::canonicalize(root), std::fs::canonicalize(file)) {
        if let Ok(relative) = file.strip_prefix(&root) {
            return path_to_slash(relative);
        }
    }
    path_to_slash(file)
}

fn path_to_slash(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

/// `datetime.fromtimestamp(seconds).isoformat()` for a supplied local offset.
///
/// Python emits no offset and prints microseconds only when they are non-zero.
pub fn format_local_timestamp(seconds: i64, micros: u32, offset_seconds: i32) -> Option<String> {
    let offset = FixedOffset::east_opt(offset_seconds)?;
    let utc: DateTime<Utc> = Utc
        .timestamp_opt(seconds, micros.checked_mul(1_000)?)
        .single()?;
    let local = utc.with_timezone(&offset);
    if micros == 0 {
        Some(local.format("%Y-%m-%dT%H:%M:%S").to_string())
    } else {
        Some(format!("{}", local.format("%Y-%m-%dT%H:%M:%S%.6f")))
    }
}

/// Convenience for callers that have a canonical file path.
pub fn file_timestamp(path: &Path) -> Option<String> {
    file_timestamp_from_metadata(&std::fs::metadata(path).ok()?)
}

/// Timestamp from an already-open file handle, so a scan never re-stats a path
/// that could have been swapped after the handle was opened.
pub fn file_timestamp_from_metadata(metadata: &std::fs::Metadata) -> Option<String> {
    let (seconds, micros) = metadata_parts(metadata)?;
    let offset = chrono::Local
        .timestamp_opt(seconds, 0)
        .single()?
        .offset()
        .local_minus_utc();
    format_local_timestamp(seconds, micros, offset)
}

/// Same conversion with an injected local offset, so the stat/rounding path can
/// be compared byte-for-byte with Python `datetime.fromtimestamp` without
/// depending on the test machine's time zone.
pub fn file_timestamp_from_metadata_with_offset(
    metadata: &std::fs::Metadata,
    offset_seconds: i32,
) -> Option<String> {
    let (seconds, micros) = metadata_parts(metadata)?;
    format_local_timestamp(seconds, micros, offset_seconds)
}

/// CPython rounds `datetime.fromtimestamp` to the nearest microsecond with
/// ties-to-even; `Duration::subsec_micros` truncates.
fn metadata_parts(metadata: &std::fs::Metadata) -> Option<(i64, u32)> {
    let modified = metadata.modified().ok()?;
    let since_epoch = modified.duration_since(std::time::UNIX_EPOCH).ok()?;
    let mut seconds = since_epoch.as_secs() as i64;
    let mut micros = round_micros(since_epoch.subsec_nanos());
    if micros == 1_000_000 {
        seconds += 1;
        micros = 0;
    }
    Some((seconds, micros))
}

/// CPython rounds `datetime.fromtimestamp` to the nearest microsecond with
/// ties-to-even; `Duration::subsec_micros` truncates.
fn round_micros(nanos: u32) -> u32 {
    let micros = nanos / 1_000;
    let remainder = nanos % 1_000;
    if remainder > 500 || (remainder == 500 && micros % 2 == 1) {
        micros + 1
    } else {
        micros
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn whitespace_only_content_has_no_calls() {
        assert_eq!(
            plan_document("tag", "a.md", " \n\t", None).unwrap(),
            Vec::new()
        );
    }

    #[test]
    fn metadata_and_tags_match_the_python_flow() {
        let content = "# Title\n\nIntro paragraph that is long enough to matter here.\n\n## Section\n\nBody body body body body body body body.\n";
        let calls = plan_document("repo", "docs/guide.md", content, None).unwrap();
        assert!(!calls.is_empty());
        for call in &calls {
            assert_eq!(call.metadata["source"], "cocoindex");
            assert_eq!(call.metadata["repo"], "repo");
            assert_eq!(call.tags, vec!["docs".to_string(), "repo".to_string()]);
            assert!(call.document_id.starts_with("repo--docs--guide"));
        }
        assert_eq!(calls[0].document_id, "repo--docs--guide");
    }

    #[test]
    fn top_level_files_use_root_tag() {
        let calls = plan_document(
            "repo",
            "README.md",
            "# T\n\nSome long body text here.",
            None,
        )
        .unwrap();
        assert_eq!(calls[0].tags[0], "root");
    }

    #[test]
    fn dotted_relative_paths_use_the_first_real_part() {
        assert_eq!(section_tag("./a/b.md"), "a");
        assert_eq!(section_tag("a//b.md"), "a");
    }

    #[test]
    fn timestamp_rounding_matches_python() {
        assert_eq!(round_micros(600), 1);
        assert_eq!(round_micros(500), 0);
        assert_eq!(round_micros(1_500), 2);
        assert_eq!(round_micros(999_999_999), 1_000_000);
    }

    #[test]
    fn absolute_paths_use_slash_section_tag() {
        let calls =
            plan_document("repo", "/a/b.md", "# T\n\nSome long body text here.", None).unwrap();
        assert_eq!(calls[0].tags[0], "/");
    }

    #[test]
    fn timestamp_format_matches_python_isoformat() {
        assert_eq!(
            format_local_timestamp(1_700_000_000, 0, 0).unwrap(),
            "2023-11-14T22:13:20"
        );
        assert_eq!(
            format_local_timestamp(1_700_000_000, 123_456, 0).unwrap(),
            "2023-11-14T22:13:20.123456"
        );
        assert_eq!(
            format_local_timestamp(1_700_000_000, 0, 3600).unwrap(),
            "2023-11-14T23:13:20"
        );
    }

    #[test]
    fn metadata_timestamp_with_injected_offset_matches_python() {
        let path =
            std::env::temp_dir().join(format!("engram-document-mtime-{}", std::process::id()));
        std::fs::write(&path, b"mtime").unwrap();
        let file = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
        let times = std::fs::FileTimes::new().set_modified(
            std::time::UNIX_EPOCH + std::time::Duration::new(1_700_000_000, 123_456_789),
        );
        file.set_times(times).unwrap();
        let metadata = std::fs::metadata(&path).unwrap();
        assert_eq!(
            file_timestamp_from_metadata_with_offset(&metadata, 0).unwrap(),
            "2023-11-14T22:13:20.123457"
        );
        assert_eq!(
            file_timestamp_from_metadata_with_offset(&metadata, 3600).unwrap(),
            "2023-11-14T23:13:20.123457"
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn relative_path_strips_the_root() {
        let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let file = root.join("src/ids.rs");
        assert_eq!(relative_path(&root, &file), "src/ids.rs");
    }
}
