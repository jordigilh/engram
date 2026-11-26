//! Ledger row types shared by reconciliation, the store, and tests.

use std::collections::BTreeMap;

use crate::hashing::ContentHash;

/// Bumped whenever the docs chunking/layout contract changes so a reconcile
/// re-emits every chunk of every file instead of trusting old hashes.
pub const LAYOUT_VERSION: i32 = 1;

/// Separate from [`LAYOUT_VERSION`] so a future issue-comment rekey cannot
/// force every docs source to re-emit its chunks.
pub const ISSUE_LAYOUT_VERSION: i32 = 1;

pub const DEFAULT_DOCUMENT_FORMAT: &str = "markdown";
pub const DEFAULT_SOURCE_KIND: &str = "docs";

#[derive(Debug, Clone, Copy)]
pub struct Migration {
    pub version: i64,
    pub sql: &'static str,
}

/// Ordered, embedded migrations. The runner applies each version once and
/// records it in `schema_migrations`; migration files must not insert their
/// own version.
pub const MIGRATIONS: &[Migration] = &[
    Migration {
        version: 1,
        sql: include_str!("../migrations/0001_ledger.sql"),
    },
    Migration {
        version: 2,
        sql: include_str!("../migrations/0002_job_error_class.sql"),
    },
    Migration {
        version: 3,
        sql: include_str!("../migrations/0003_bank_ids.sql"),
    },
    Migration {
        version: 4,
        sql: include_str!("../migrations/0004_bank_document_lease.sql"),
    },
    Migration {
        version: 5,
        sql: include_str!("../migrations/0005_issue_sources.sql"),
    },
];

/// Hindsight bank ids become one URL path segment, so keep the accepted shape
/// explicit instead of relying on percent-encoding at the HTTP boundary.
pub fn is_valid_bank_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value != "."
        && value != ".."
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceRecord {
    pub project: String,
    pub tag: String,
    pub root: String,
    pub bank_id: String,
    pub docs_include: Vec<String>,
    pub docs_exclude: Vec<String>,
    pub document_format: String,
    pub source_kind: String,
}

impl SourceRecord {
    pub fn markdown(
        project: &str,
        tag: &str,
        root: &str,
        bank_id: &str,
        includes: &[&str],
    ) -> Self {
        Self {
            project: project.to_string(),
            tag: tag.to_string(),
            root: root.to_string(),
            bank_id: bank_id.to_string(),
            docs_include: includes.iter().map(|value| value.to_string()).collect(),
            docs_exclude: Vec::new(),
            document_format: DEFAULT_DOCUMENT_FORMAT.to_string(),
            source_kind: DEFAULT_SOURCE_KIND.to_string(),
        }
    }

    /// One issue stream (provider + repo + item kind, or Jira project/JQL).
    pub fn issue(project: &str, tag: &str, root: &str, bank_id: &str, source_kind: &str) -> Self {
        Self {
            project: project.to_string(),
            tag: tag.to_string(),
            root: root.to_string(),
            bank_id: bank_id.to_string(),
            docs_include: Vec::new(),
            docs_exclude: Vec::new(),
            document_format: "issue".to_string(),
            source_kind: source_kind.to_string(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum RecordState {
    Active,
    Tombstoned,
}

impl RecordState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Active => "active",
            Self::Tombstoned => "tombstoned",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "active" => Some(Self::Active),
            "tombstoned" => Some(Self::Tombstoned),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestDocument {
    pub relative_path: String,
    pub content_sha256: ContentHash,
    pub size_bytes: i64,
    pub layout_version: i32,
    pub state: RecordState,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestChunk {
    pub document_path: String,
    pub hindsight_document_id: String,
    pub chunk_key: String,
    pub content_sha256: ContentHash,
    pub ordinal: i32,
    pub generation: i32,
    pub state: RecordState,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Manifest {
    pub documents: BTreeMap<String, ManifestDocument>,
    /// Keyed by the Hindsight document id.
    pub chunks: BTreeMap<String, ManifestChunk>,
    /// Monotonic source revision this snapshot was read at.
    pub revision: i64,
}

impl Manifest {
    pub fn chunks_for<'a>(
        &'a self,
        relative_path: &'a str,
    ) -> impl Iterator<Item = &'a ManifestChunk> + 'a {
        self.chunks
            .values()
            .filter(move |chunk| chunk.document_path == relative_path)
    }
}
