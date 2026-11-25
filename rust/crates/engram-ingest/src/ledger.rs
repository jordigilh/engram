//! Ledger row types shared by reconciliation, the store, and tests.

use std::collections::BTreeMap;

use crate::hashing::ContentHash;

/// Bumped whenever the docs chunking/layout contract changes so a reconcile
/// re-emits every chunk of every file instead of trusting old hashes.
pub const LAYOUT_VERSION: i32 = 1;

pub const DEFAULT_DOCUMENT_FORMAT: &str = "markdown";

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
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceRecord {
    pub project: String,
    pub tag: String,
    pub root: String,
    pub docs_include: Vec<String>,
    pub docs_exclude: Vec<String>,
    pub document_format: String,
}

impl SourceRecord {
    pub fn markdown(project: &str, tag: &str, root: &str, includes: &[&str]) -> Self {
        Self {
            project: project.to_string(),
            tag: tag.to_string(),
            root: root.to_string(),
            docs_include: includes.iter().map(|value| value.to_string()).collect(),
            docs_exclude: Vec::new(),
            document_format: DEFAULT_DOCUMENT_FORMAT.to_string(),
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
