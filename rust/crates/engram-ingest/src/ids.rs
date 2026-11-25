//! Stable document and section identifiers ported from the Python docs flow.
//!
//! The base id is `"{source_tag}--{relative path without suffix, / -> --}"`
//! (`src/engram/flows/configured.py::_document_base_id`). Section keys are the
//! first 16 hex characters of the SHA-1 of the heading line
//! (`src/engram/chunking.py::_stable_key`).

use sha1::{Digest, Sha1};

/// `pathlib.PurePath.suffix` as implemented by Python 3.14, the interpreter the
/// deployed `engram-flows-configured` venv runs.
///
/// Python 3.13 differs only for names whose last component ends in a dot
/// (`"a."` -> `""` on 3.13, `"."` on 3.14). Goldens pin 3.14; see the crate
/// README.
pub fn pathlib_suffix(path: &str) -> &str {
    let name = posix_name(path);
    let trimmed = name.trim_start_matches('.');
    match trimmed.rfind('.') {
        Some(index) => &trimmed[index..],
        None => "",
    }
}

fn posix_name(path: &str) -> &str {
    let trimmed = path.trim_end_matches('/');
    if trimmed.is_empty() {
        return "";
    }
    match trimmed.rfind('/') {
        Some(index) => &trimmed[index + 1..],
        None => trimmed,
    }
}

/// Stable base document id for a docs source file.
pub fn document_base_id(source_tag: &str, relative_path: &str) -> String {
    let suffix = pathlib_suffix(relative_path);
    let name = if suffix.is_empty() {
        relative_path
    } else {
        &relative_path[..relative_path.len() - suffix.len()]
    };
    format!("{}--{}", source_tag, name.replace('/', "--"))
}

/// Final Hindsight document id for a chunk: the bare base id for the `""` key,
/// otherwise `{base}--{key}`.
pub fn chunk_document_id(base_id: &str, chunk_key: &str) -> String {
    if chunk_key.is_empty() {
        base_id.to_string()
    } else {
        format!("{base_id}--{chunk_key}")
    }
}

/// 16-hex-character SHA-1 of a heading line, used as a section key.
pub fn stable_section_key(heading_line: &str) -> String {
    let digest = Sha1::digest(heading_line.as_bytes());
    let hex = format!("{digest:x}");
    hex[..16].to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suffix_matches_python_314() {
        let cases = [
            ("a/b.md", ".md"),
            ("docs/x.tar.gz", ".gz"),
            ("a.", "."),
            (".bashrc", ""),
            ("a..b", ".b"),
            ("a..", "."),
            ("...", ""),
            ("..", ""),
            ("a/b/c", ""),
            ("noext", ""),
            ("dir/.hidden.md", ".md"),
        ];
        for (path, expected) in cases {
            assert_eq!(pathlib_suffix(path), expected, "suffix for {path}");
        }
    }

    #[test]
    fn base_ids_strip_only_the_suffix() {
        assert_eq!(document_base_id("engram", "a/b.md"), "engram--a--b");
        assert_eq!(
            document_base_id("repo", "docs/x.tar.gz"),
            "repo--docs--x.tar"
        );
        assert_eq!(document_base_id("repo", "a."), "repo--a");
        assert_eq!(document_base_id("repo", "noext"), "repo--noext");
        assert_eq!(document_base_id("repo", "/a/b.md"), "repo----a--b");
    }

    #[test]
    fn chunk_ids_follow_the_python_concat_rule() {
        let base = document_base_id("engram", "a/b.md");
        assert_eq!(chunk_document_id(&base, ""), "engram--a--b");
        assert_eq!(
            chunk_document_id(&base, "0123456789abcdef"),
            "engram--a--b--0123456789abcdef"
        );
        assert_eq!(chunk_document_id(&base, "part1"), "engram--a--b--part1");
    }

    #[test]
    fn section_keys_are_sha1_prefixes() {
        assert_eq!(stable_section_key("## Section A"), "609c1dffe2729c7b");
        assert_eq!(stable_section_key("## Section A").len(), 16);
        assert_ne!(
            stable_section_key("## Section A"),
            stable_section_key("## Section B")
        );
    }
}
