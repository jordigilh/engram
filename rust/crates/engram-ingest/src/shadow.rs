//! Tree-level shadow planning for the docs flow (issue #214 slice 3).
//!
//! Emits the exact retain operations the Python flow would emit for a frozen
//! Markdown tree, in a canonical JSONL form that the Python reference generator
//! reproduces from the real CocoIndex walker and `_retain_document`.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::document::{file_timestamp, plan_document, relative_path};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShadowOptions {
    pub source_tag: String,
    /// Fixture mode disables mtimes so the expected file is reproducible.
    pub timestamps: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ShadowOperation {
    pub path: String,
    pub ordinal: usize,
    pub document_id: String,
    pub content: String,
    pub content_sha256: String,
    pub metadata: BTreeMap<String, String>,
    pub tags: Vec<String>,
    pub timestamp: Option<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum ShadowError {
    #[error("filesystem error at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error(transparent)]
    Synthesis(#[from] crate::synthesis::SynthesisError),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error("root is not a directory: {0}")]
    NotADirectory(PathBuf),
}

pub fn plan_tree(
    root: &Path,
    options: &ShadowOptions,
) -> Result<Vec<ShadowOperation>, ShadowError> {
    if !root.is_dir() {
        return Err(ShadowError::NotADirectory(root.to_path_buf()));
    }
    let mut files = Vec::new();
    collect_markdown(root, &mut files)?;
    files.sort_by_cached_key(|path| relative_path(root, path));

    let mut operations = Vec::new();
    for file in files {
        let bytes = std::fs::read(&file).map_err(|source| ShadowError::Io {
            path: file.clone(),
            source,
        })?;
        let content = decode_text(&bytes);
        if content.trim().is_empty() {
            continue;
        }
        let relative = relative_path(root, &file);
        let timestamp = options.timestamps.then(|| file_timestamp(&file)).flatten();
        for (ordinal, call) in plan_document(&options.source_tag, &relative, &content, timestamp)?
            .into_iter()
            .enumerate()
        {
            operations.push(ShadowOperation {
                path: relative.clone(),
                ordinal,
                document_id: call.document_id,
                content: call.content,
                content_sha256: call.content_sha256.to_hex(),
                metadata: call.metadata,
                tags: call.tags,
                timestamp: call.timestamp,
            });
        }
    }
    operations.sort_by_key(|operation| (operation.path.clone(), operation.ordinal));
    Ok(operations)
}

fn collect_markdown(root: &Path, files: &mut Vec<PathBuf>) -> Result<(), ShadowError> {
    let entries = std::fs::read_dir(root).map_err(|source| ShadowError::Io {
        path: root.to_path_buf(),
        source,
    })?;
    for entry in entries {
        let entry = entry.map_err(|source| ShadowError::Io {
            path: root.to_path_buf(),
            source,
        })?;
        let path = entry.path();
        let file_type = entry.file_type().map_err(|source| ShadowError::Io {
            path: path.clone(),
            source,
        })?;
        if file_type.is_dir() {
            collect_markdown(&path, files)?;
        } else if path.is_file() && path.extension().is_some_and(|ext| ext == "md") {
            // `Path::is_file` follows file symlinks like CocoIndex; symlinked
            // directories are not recursed, so cycles stay impossible.
            files.push(path);
        }
    }
    Ok(())
}

/// Match CocoIndex 1.0.26 `read_text`: BOM-detected UTF-16 and replacement
/// decoding instead of failing the whole run. The BOM is preserved as
/// U+FEFF (the deployed Python decoder does not strip it).
fn decode_text(bytes: &[u8]) -> String {
    if bytes.starts_with(&[0xff, 0xfe]) {
        return decode_utf16(bytes, true);
    }
    if bytes.starts_with(&[0xfe, 0xff]) {
        return decode_utf16(bytes, false);
    }
    String::from_utf8_lossy(bytes).into_owned()
}

fn decode_utf16(bytes: &[u8], little_endian: bool) -> String {
    let units = (0..bytes.len() / 2).map(|index| {
        let pair = [bytes[index * 2], bytes[index * 2 + 1]];
        if little_endian {
            u16::from_le_bytes(pair)
        } else {
            u16::from_be_bytes(pair)
        }
    });
    char::decode_utf16(units)
        .map(|result| result.unwrap_or(char::REPLACEMENT_CHARACTER))
        .collect()
}

/// Canonical JSONL: sorted keys (serde_json maps are `BTreeMap`), compact, one
/// operation per line, trailing newline. The Python generator uses
/// `json.dumps(..., sort_keys=True, separators=(",", ":"))`.
pub fn to_jsonl(operations: &[ShadowOperation]) -> Result<String, ShadowError> {
    let mut output = String::new();
    for operation in operations {
        output.push_str(&serde_json::to_string(&serde_json::to_value(operation)?)?);
        output.push('\n');
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/shadow/tree")
    }

    fn options() -> ShadowOptions {
        ShadowOptions {
            source_tag: "repo".to_string(),
            timestamps: false,
        }
    }

    #[test]
    fn tree_is_deterministic_and_skips_empty_and_non_markdown() {
        let first = plan_tree(&fixture_root(), &options()).unwrap();
        let second = plan_tree(&fixture_root(), &options()).unwrap();
        assert_eq!(first, second);
        assert!(!first.iter().any(|op| op.path == "skip.txt"));
        assert!(!first.iter().any(|op| op.path == "empty.md"));
        assert!(first.iter().any(|op| op.path == "nested/deep/ref.md"));
        let mut sorted = first.clone();
        sorted.sort_by_key(|op| (op.path.clone(), op.ordinal));
        assert_eq!(first, sorted);
    }

    #[test]
    fn jsonl_is_compact_sorted_and_newline_terminated() {
        let operations = plan_tree(&fixture_root(), &options()).unwrap();
        let jsonl = to_jsonl(&operations).unwrap();
        assert!(jsonl.ends_with('\n'));
        let first = jsonl.lines().next().unwrap();
        assert!(first.starts_with("{\"content\":"));
        assert!(!first.contains(": "));
        assert_eq!(jsonl.lines().count(), operations.len());
    }

    #[test]
    fn bom_text_decoding_matches_read_text() {
        assert_eq!(decode_text(b"\xef\xbb\xbfhello"), "\u{feff}hello");
        let mut utf16 = vec![0xff, 0xfe];
        for unit in "hé".encode_utf16() {
            utf16.extend_from_slice(&unit.to_le_bytes());
        }
        assert_eq!(decode_text(&utf16), "\u{feff}hé");
        assert_eq!(decode_text(b"bad\xffbyte"), "bad\u{fffd}byte");
    }

    #[test]
    fn missing_root_is_an_error() {
        let error = plan_tree(Path::new("/definitely/missing"), &options()).unwrap_err();
        assert!(matches!(error, ShadowError::NotADirectory(_)));
    }
}
