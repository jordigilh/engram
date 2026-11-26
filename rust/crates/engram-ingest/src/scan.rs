//! Filesystem scan for the docs runtime (issue #242).
//!
//! The scan is the source of truth for reconciliation, so it is deliberately
//! conservative: a walk or read failure aborts the whole source snapshot
//! instead of returning a partial list that would tombstone surviving files.
//! Only a missing root is a soft skip, matching the Python flow's warning.
//!
//! Matching follows `PatternFilePathMatcher` where it changes behavior:
//! excludes are gitignore-shaped and ordered, `!` re-includes, and excluding a
//! directory excludes its descendants. Directory pruning is skipped when a
//! negation exists, which keeps re-included subtrees reachable.

use std::fmt::Write as _;
use std::io::Read;
use std::path::{Path, PathBuf};

use globset::{GlobBuilder, GlobMatcher};

use crate::config::DocsSource;
use crate::document::{file_timestamp, plan_document};
use crate::hashing::ContentHash;
use crate::reconcile::ScannedFile;
use crate::synthesis::SynthesisError;
use crate::worker::Shutdown;

#[derive(Debug, thiserror::Error)]
pub enum ScanError {
    #[error("source root does not exist or is not a directory: {0}")]
    MissingRoot(PathBuf),
    #[error("scan cancelled")]
    Cancelled,
    #[error("cannot read directory {path}: {source}")]
    Walk {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("cannot read file {path}: {source}")]
    Read {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid path pattern {pattern:?}: {source}")]
    Pattern {
        pattern: String,
        #[source]
        source: globset::Error,
    },
    #[error("two files collapse to the same lossy relative path: {0}")]
    PathCollision(String),
    #[error("cannot plan document {path}: {source}")]
    Plan {
        path: PathBuf,
        #[source]
        source: SynthesisError,
    },
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ScanReport {
    pub matched_files: usize,
    pub skipped_symlinks: usize,
    pub pruned_directories: usize,
}

/// Scan one configured docs root into reconciliation input.
pub fn scan_source(source: &DocsSource) -> Result<(Vec<ScannedFile>, ScanReport), ScanError> {
    scan_source_with_shutdown(source, None)
}

pub fn scan_source_with_shutdown(
    source: &DocsSource,
    shutdown: Option<&Shutdown>,
) -> Result<(Vec<ScannedFile>, ScanReport), ScanError> {
    if !source.root.is_dir() {
        return Err(ScanError::MissingRoot(source.root.clone()));
    }
    let patterns = Patterns::new(&source.docs_include, &source.docs_exclude)?;
    let canonical_root = std::fs::canonicalize(&source.root).map_err(|error| ScanError::Walk {
        path: source.root.clone(),
        source: error,
    })?;
    let mut files = Vec::new();
    let mut report = ScanReport::default();
    Walker {
        root: &source.root,
        canonical_root: &canonical_root,
        patterns: &patterns,
        source,
        shutdown,
        files: &mut files,
        report: &mut report,
    }
    .walk(&source.root)?;
    let mut seen = std::collections::BTreeSet::new();
    for file in &files {
        if !seen.insert(file.relative_path.clone()) {
            return Err(ScanError::PathCollision(file.relative_path.clone()));
        }
    }
    Ok((files, report))
}

struct Walker<'a> {
    root: &'a Path,
    canonical_root: &'a Path,
    patterns: &'a Patterns,
    source: &'a DocsSource,
    shutdown: Option<&'a Shutdown>,
    files: &'a mut Vec<ScannedFile>,
    report: &'a mut ScanReport,
}

impl Walker<'_> {
    fn walk(&mut self, directory: &Path) -> Result<(), ScanError> {
        if self.shutdown.is_some_and(Shutdown::is_cancelled) {
            return Err(ScanError::Cancelled);
        }
        let mut entries = std::fs::read_dir(directory)
            .map_err(|source| ScanError::Walk {
                path: directory.to_path_buf(),
                source,
            })?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|source| ScanError::Walk {
                path: directory.to_path_buf(),
                source,
            })?;
        entries.sort_by_key(|entry| entry.file_name());

        for entry in entries {
            if self.shutdown.is_some_and(Shutdown::is_cancelled) {
                return Err(ScanError::Cancelled);
            }
            let path = entry.path();
            let file_type = entry.file_type().map_err(|source| ScanError::Walk {
                path: path.clone(),
                source,
            })?;
            // Never follow links: a symlink can loop or escape the configured root.
            if file_type.is_symlink() {
                self.report.skipped_symlinks += 1;
                continue;
            }
            let relative = relative_posix(self.root, &path);
            if file_type.is_dir() {
                if self.patterns.prune_directory(&relative) {
                    self.report.pruned_directories += 1;
                    continue;
                }
                // Re-check after the walk's lstat: a directory can be swapped for
                // a symlink between the entry and the recursive read.
                let current =
                    std::fs::symlink_metadata(&path).map_err(|error| ScanError::Walk {
                        path: path.clone(),
                        source: error,
                    })?;
                let canonical = std::fs::canonicalize(&path).map_err(|error| ScanError::Walk {
                    path: path.clone(),
                    source: error,
                })?;
                if current.file_type().is_symlink() || !canonical.starts_with(self.canonical_root) {
                    self.report.skipped_symlinks += 1;
                    continue;
                }
                self.walk(&path)?;
                continue;
            }
            if !file_type.is_file() || !self.patterns.includes_file(&relative) {
                continue;
            }
            if self.patterns.excludes_file(&relative) {
                continue;
            }
            let bytes = read_regular_file(&path).map_err(|source| ScanError::Read {
                path: path.clone(),
                source,
            })?;
            let content = decode(&bytes);
            let calls = plan_document(&self.source.tag, &relative, &content, file_timestamp(&path))
                .map_err(|source| ScanError::Plan {
                    path: path.clone(),
                    source,
                })?;
            self.report.matched_files += 1;
            self.files.push(ScannedFile {
                relative_path: relative,
                content_sha256: ContentHash::of(content.as_bytes()),
                size_bytes: bytes.len() as i64,
                calls,
            });
        }
        Ok(())
    }
}

/// Read a regular file without following a symlink swapped in after the walk
/// checked the entry type.
#[cfg(unix)]
fn read_regular_file(path: &Path) -> std::io::Result<Vec<u8>> {
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    if !file.metadata()?.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "not a regular file",
        ));
    }
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    Ok(bytes)
}

#[cfg(not(unix))]
fn read_regular_file(path: &Path) -> std::io::Result<Vec<u8>> {
    if !std::fs::metadata(path)?.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "not a regular file",
        ));
    }
    std::fs::read(path)
}

/// Relative path with `/` separators, built from the walk instead of
/// `canonicalize` so a link or canonicalization failure cannot leak an
/// absolute path into a Hindsight document id.
fn relative_posix(root: &Path, path: &Path) -> String {
    let relative = path.strip_prefix(root).unwrap_or(path);
    let mut output = String::new();
    for component in relative.components() {
        if !output.is_empty() {
            output.push('/');
        }
        let _ = write!(output, "{}", component.as_os_str().to_string_lossy());
    }
    output
}

/// Decode like CocoIndex's `read_text`: BOM-aware UTF-8/UTF-16 with lossy
/// replacement. Only real I/O errors are fatal.
fn decode(bytes: &[u8]) -> String {
    if let Some(rest) = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]) {
        return String::from_utf8_lossy(rest).into_owned();
    }
    if let Some(rest) = bytes.strip_prefix(&[0xFF, 0xFE]) {
        return decode_utf16(rest, true);
    }
    if let Some(rest) = bytes.strip_prefix(&[0xFE, 0xFF]) {
        return decode_utf16(rest, false);
    }
    String::from_utf8_lossy(bytes).into_owned()
}

fn decode_utf16(bytes: &[u8], little_endian: bool) -> String {
    let mut units = Vec::with_capacity(bytes.len() / 2);
    for pair in bytes.chunks(2) {
        if let [first, second] = pair {
            units.push(if little_endian {
                u16::from_le_bytes([*first, *second])
            } else {
                u16::from_be_bytes([*first, *second])
            });
        }
    }
    char::decode_utf16(units)
        .map(|character| character.unwrap_or(char::REPLACEMENT_CHARACTER))
        .collect()
}

struct Patterns {
    includes: Vec<GlobMatcher>,
    excludes: Vec<(bool, GlobMatcher)>,
    has_negation: bool,
}

impl Patterns {
    fn new(includes: &[String], excludes: &[String]) -> Result<Self, ScanError> {
        let includes = includes
            .iter()
            .map(|pattern| compile(pattern))
            .collect::<Result<Vec<_>, _>>()?;
        let mut matches = Vec::with_capacity(excludes.len());
        let mut has_negation = false;
        for pattern in excludes {
            let (negated, raw) = match pattern.strip_prefix('!') {
                Some(rest) => {
                    has_negation = true;
                    (true, rest)
                }
                None => (false, pattern.as_str()),
            };
            matches.push((negated, compile(raw)?));
        }
        Ok(Self {
            includes,
            excludes: matches,
            has_negation,
        })
    }

    fn includes_file(&self, relative: &str) -> bool {
        self.includes.iter().any(|glob| glob.is_match(relative))
    }

    fn excludes_file(&self, relative: &str) -> bool {
        if self.excludes.is_empty() {
            return false;
        }
        let ancestors = ancestor_prefixes(relative);
        let mut excluded = false;
        for (negated, glob) in &self.excludes {
            let matches = glob.is_match(relative)
                || ancestors
                    .iter()
                    .any(|directory| matches_directory(glob, directory));
            if matches {
                excluded = !negated;
            }
        }
        excluded
    }

    fn prune_directory(&self, relative: &str) -> bool {
        if self.has_negation {
            return false;
        }
        self.excludes
            .iter()
            .fold(false, |excluded, (negated, glob)| {
                if matches_directory(glob, relative) {
                    !negated
                } else {
                    excluded
                }
            })
    }
}

fn compile(pattern: &str) -> Result<GlobMatcher, ScanError> {
    GlobBuilder::new(pattern)
        .literal_separator(true)
        .build()
        .map(|glob| glob.compile_matcher())
        .map_err(|source| ScanError::Pattern {
            pattern: pattern.to_string(),
            source,
        })
}

/// Validate include/exclude globs at config-load time so a typo fails startup
/// instead of leaving a healthy process that ingests nothing.
pub fn validate_patterns(includes: &[String], excludes: &[String]) -> Result<(), ScanError> {
    Patterns::new(includes, excludes).map(|_| ())
}

/// A directory match covers the directory itself and everything below it.
fn matches_directory(glob: &GlobMatcher, relative: &str) -> bool {
    glob.is_match(relative)
        || glob.is_match(format!("{relative}/"))
        || glob.is_match(format!("{relative}/__engram_probe__"))
}

/// Every parent prefix of a relative path, excluding the file itself.
fn ancestor_prefixes(relative: &str) -> Vec<&str> {
    let mut prefixes = Vec::new();
    for (index, character) in relative.char_indices() {
        if character == '/' {
            prefixes.push(&relative[..index]);
        }
    }
    prefixes
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    fn fixture() -> PathBuf {
        let unique = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "engram-ingest-scan-{}-{unique}",
            std::process::id()
        ));
        std::fs::create_dir_all(&root).expect("root");
        root
    }

    fn write(root: &Path, relative: &str, content: &[u8]) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("parent")).expect("dirs");
        std::fs::write(path, content).expect("write");
    }

    fn source(root: &Path, include: &[&str], exclude: &[&str]) -> DocsSource {
        DocsSource {
            project: "example".to_string(),
            tag: "repo".to_string(),
            root: root.to_path_buf(),
            bank_id: "example-docs".to_string(),
            docs_include: include.iter().map(|value| value.to_string()).collect(),
            docs_exclude: exclude.iter().map(|value| value.to_string()).collect(),
        }
    }

    #[test]
    fn scans_included_files_in_deterministic_order() {
        let root = fixture();
        write(&root, "b.md", b"# B\n\ncontent");
        write(&root, "a.md", b"# A\n\ncontent");
        write(&root, "nested/c.md", b"# C\n\ncontent");
        write(&root, "nested/skip.txt", b"text");
        let (files, report) = scan_source(&source(&root, &["**/*.md"], &[])).expect("scan");
        let paths: Vec<&str> = files
            .iter()
            .map(|file| file.relative_path.as_str())
            .collect();
        assert_eq!(paths, vec!["a.md", "b.md", "nested/c.md"]);
        assert_eq!(report.matched_files, 3);
        assert!(files.iter().all(|file| !file.calls.is_empty()));
    }

    #[test]
    fn excludes_files_and_prunes_directory_patterns() {
        let root = fixture();
        write(&root, "keep/a.md", b"keep");
        write(&root, "vendor/b.md", b"vendor");
        write(&root, "nested/vendor/c.md", b"nested vendor");
        write(&root, "node_modules/d.md", b"modules");
        let (files, report) = scan_source(&source(
            &root,
            &["**/*.md"],
            &["**/vendor", "**/node_modules/**"],
        ))
        .expect("scan");
        let paths: Vec<&str> = files
            .iter()
            .map(|file| file.relative_path.as_str())
            .collect();
        assert_eq!(paths, vec!["keep/a.md"]);
        assert_eq!(report.pruned_directories, 3);
    }

    #[test]
    fn negation_reincludes_subtree_without_pruning() {
        let root = fixture();
        write(&root, "vendor/drop.md", b"drop");
        write(&root, "vendor/keep/a.md", b"keep");
        write(&root, ".hidden/b.md", b"hidden");
        write(&root, ".github/c.md", b"workflow");
        let (files, report) = scan_source(&source(
            &root,
            &["**/*.md"],
            &[
                "**/vendor/**",
                "!**/vendor/keep/**",
                "**/.*",
                "!**/.github/**",
            ],
        ))
        .expect("scan");
        let paths: Vec<&str> = files
            .iter()
            .map(|file| file.relative_path.as_str())
            .collect();
        assert_eq!(paths, vec![".github/c.md", "vendor/keep/a.md"]);
        assert_eq!(report.pruned_directories, 0);
    }

    #[test]
    fn blank_files_stay_in_snapshot_without_calls() {
        let root = fixture();
        write(&root, "empty.md", b"   \n");
        let (files, _) = scan_source(&source(&root, &["**/*.md"], &[])).expect("scan");
        assert_eq!(files.len(), 1);
        assert!(files[0].calls.is_empty());
        assert_eq!(files[0].size_bytes, 4);
    }

    #[test]
    fn decodes_boms_and_invalid_utf8_lossily() {
        let root = fixture();
        let mut utf16 = vec![0xFF, 0xFE];
        for unit in "# UTF16\n\nhello".encode_utf16() {
            utf16.extend_from_slice(&unit.to_le_bytes());
        }
        write(&root, "utf16.md", &utf16);
        write(&root, "invalid.md", b"# Bad\n\n\xFF\xFEtext");
        write(&root, "utf8bom.md", b"\xEF\xBB\xBF# BOM\n\nbody");
        let (files, _) = scan_source(&source(&root, &["**/*.md"], &[])).expect("scan");
        assert_eq!(files.len(), 3);
        assert!(
            files.iter().all(|file| !file.calls.is_empty()),
            "all documents should plan"
        );
    }

    #[test]
    fn missing_root_is_a_soft_skip() {
        let root = fixture().join("missing");
        let error = scan_source(&source(&root, &["**/*.md"], &[])).expect_err("missing");
        assert!(matches!(error, ScanError::MissingRoot(_)));
    }

    #[test]
    fn invalid_pattern_and_directory_are_errors() {
        let root = fixture();
        let error = scan_source(&source(&root, &["["], &[])).expect_err("pattern");
        assert!(matches!(error, ScanError::Pattern { .. }));

        let file = fixture().join("file");
        std::fs::write(&file, b"not a directory").expect("file");
        let error = scan_source(&source(&file, &["**/*.md"], &[])).expect_err("not dir");
        assert!(matches!(error, ScanError::MissingRoot(_)));
    }

    #[test]
    fn absolute_paths_never_leak_into_relative_paths() {
        let root = fixture();
        write(&root, "a/b.md", b"body");
        let (files, _) = scan_source(&source(&root, &["**/*.md"], &[])).expect("scan");
        assert_eq!(files[0].relative_path, "a/b.md");
        assert!(!files[0].relative_path.starts_with('/'));
        assert!(!files[0].relative_path.contains('\\'));
    }

    #[cfg(unix)]
    #[test]
    fn skips_symlinked_files_and_directories() {
        let root = fixture();
        write(&root, "real/a.md", b"body");
        let outside = fixture();
        write(&outside, "outside.md", b"outside");
        std::os::unix::fs::symlink(outside.join("outside.md"), root.join("link.md"))
            .expect("file link");
        std::os::unix::fs::symlink(&outside, root.join("linked-dir")).expect("dir link");
        let (files, report) = scan_source(&source(&root, &["**/*.md"], &[])).expect("scan");
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].relative_path, "real/a.md");
        assert_eq!(report.skipped_symlinks, 2);
    }

    #[test]
    fn pattern_helpers_cover_ancestors_and_probe_paths() {
        let patterns =
            Patterns::new(&["**/*.md".to_string()], &["**/vendor".to_string()]).expect("patterns");
        assert!(patterns.includes_file("a.md"));
        assert!(patterns.includes_file("nested/a.md"));
        assert!(patterns.excludes_file("vendor"));
        assert!(patterns.excludes_file("vendor/a.md"));
        assert!(patterns.excludes_file("nested/vendor/a.md"));
        assert!(!patterns.excludes_file("keep/a.md"));
        assert!(patterns.prune_directory("nested/vendor"));
        assert_eq!(ancestor_prefixes("a/b/c.md"), vec!["a", "a/b"]);
        assert!(ancestor_prefixes("a.md").is_empty());
    }

    #[test]
    fn single_star_does_not_cross_path_separators() {
        let root = fixture();
        write(&root, "a.md", b"root");
        write(&root, "nested/b.md", b"nested");
        write(&root, "vendor/direct.md", b"direct");
        write(&root, "vendor/deep/c.md", b"deep");
        let (files, _) = scan_source(&source(&root, &["*.md"], &[])).expect("scan");
        let paths: Vec<&str> = files
            .iter()
            .map(|file| file.relative_path.as_str())
            .collect();
        assert_eq!(paths, vec!["a.md"]);

        let (files, _) = scan_source(&source(&root, &["**/*.md"], &["vendor/*.md"])).expect("scan");
        let paths: Vec<&str> = files
            .iter()
            .map(|file| file.relative_path.as_str())
            .collect();
        assert_eq!(paths, vec!["a.md", "nested/b.md", "vendor/deep/c.md"]);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn invalid_utf8_names_that_collapse_are_rejected() {
        use std::ffi::OsStr;
        use std::os::unix::ffi::OsStrExt;
        let root = fixture();
        std::fs::write(root.join(OsStr::from_bytes(b"a\xff.md")), b"one").expect("first");
        std::fs::write(root.join(OsStr::from_bytes(b"a\xfe.md")), b"two").expect("second");
        let error = scan_source(&source(&root, &["**/*.md"], &[])).expect_err("collision");
        assert!(matches!(error, ScanError::PathCollision(_)));
    }

    #[test]
    fn empty_include_never_matches() {
        let root = fixture();
        write(&root, "a.md", b"body");
        let patterns = Patterns::new(&[], &[]).expect("patterns");
        assert!(!patterns.includes_file("a.md"));
        assert!(!patterns.excludes_file("a.md"));
        assert!(!patterns.prune_directory("a"));
        drop(root);
    }
}
