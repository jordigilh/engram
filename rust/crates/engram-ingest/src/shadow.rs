//! Shadow comparison between the real Python/CocoIndex docs flow and the
//! native docs planner (issue #214 slice 3).
//!
//! This module is deliberately data-in/data-out: the oracle JSON is produced
//! by `fixtures/docs_shadow_oracle.py`, which runs the real
//! `engram.flows.configured` `docs` app against a loopback capture server.
//! The Rust side consumes a normal [`scan_source`](crate::scan::scan_source)
//! snapshot. Nothing here invokes Python, and no fixture is generated from the
//! planner itself.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::config::DocsSource;
use crate::hashing::ContentHash;
use crate::ledger::{Manifest, ManifestChunk, ManifestDocument, RecordState, LAYOUT_VERSION};
use crate::reconcile::{DeleteChunk, Reconciliation, ScannedFile};

/// One frozen docs tree as captured by the oracle script.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct OracleDump {
    pub provenance: OracleProvenance,
    pub files: Vec<OracleFile>,
    pub retains: Vec<OracleRetain>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct OracleProvenance {
    pub python: String,
    pub cocoindex: String,
    pub sklearn: String,
    pub networkx: String,
    pub flow_source_commit: String,
    #[serde(default)]
    pub flow_source_dirty: bool,
    #[serde(default)]
    pub flow_source_sha256: BTreeMap<String, String>,
    pub generated_at: String,
    pub tree: OracleTree,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct OracleTree {
    pub root: String,
    pub source_tag: String,
    pub bank_id: String,
    pub docs_include: Vec<String>,
    pub docs_exclude: Vec<String>,
    pub files: usize,
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct OracleFile {
    pub relative_path: String,
    pub base_id: String,
    /// SHA-256 of the decoded document text, matching `ScannedFile`.
    pub content_sha256: String,
    pub size_bytes: i64,
    pub timestamp: Option<String>,
}

/// One retain call exactly as it arrived at the capture server, annotated with
/// the source file and chunk key derived from the flow's own base-id helper.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct OracleRetain {
    pub sequence: u64,
    pub bank_id: String,
    pub document_id: String,
    pub relative_path: String,
    pub chunk_key: String,
    pub content: String,
    pub content_sha256: String,
    pub metadata: BTreeMap<String, String>,
    pub tags: Vec<String>,
    pub timestamp: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DivergenceKind {
    SourceTag,
    SourceRoot,
    SourceBank,
    SourcePatterns,
    MissingFile,
    ExtraFile,
    FileContent,
    FileSize,
    FileTimestamp,
    MissingRetain,
    ExtraRetain,
    RetainPath,
    ChunkKey,
    Content,
    Metadata,
    Tags,
    Timestamp,
    Bank,
    Order,
    MissingDelete,
    ExtraDelete,
    DeleteReason,
    DeleteContent,
    DuplicateCollapse,
    Conflict,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Divergence {
    pub kind: DivergenceKind,
    pub relative_path: Option<String>,
    pub document_id: Option<String>,
    pub detail: String,
}

impl Divergence {
    fn new(kind: DivergenceKind, detail: impl Into<String>) -> Self {
        Self {
            kind,
            relative_path: None,
            document_id: None,
            detail: detail.into(),
        }
    }

    fn at(mut self, path: &str) -> Self {
        self.relative_path = Some(path.to_string());
        self
    }

    fn for_document(mut self, path: &str, document_id: &str) -> Self {
        self.relative_path = Some(path.to_string());
        self.document_id = Some(document_id.to_string());
        self
    }
}

/// Summary numbers for the report; divergences are listed separately.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct FileParity {
    pub oracle_files: usize,
    pub rust_files: usize,
    pub matched: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct RetainParity {
    pub oracle_retains: usize,
    pub rust_retains: usize,
    pub matched: usize,
    /// Document ids the oracle emitted more than once because a file repeats a
    /// heading line. The ledger intentionally keeps the last occurrence.
    pub duplicate_ids: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct DeleteParity {
    pub expected: usize,
    pub emitted: usize,
    pub matched: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct ShadowReport {
    pub files: FileParity,
    pub retains: RetainParity,
    pub deletes: DeleteParity,
    pub divergences: Vec<Divergence>,
    /// Divergences that are not the documented duplicate-heading collapse.
    pub unexpected: usize,
}

impl ShadowReport {
    fn note(&mut self, divergence: Divergence) {
        if divergence.kind != DivergenceKind::DuplicateCollapse {
            self.unexpected += 1;
        }
        self.divergences.push(divergence);
    }
}

/// Compare a Rust scan snapshot with one oracle capture of the same tree.
pub fn compare(source: &DocsSource, oracle: &OracleDump, scan: &[ScannedFile]) -> ShadowReport {
    let mut report = ShadowReport::default();
    compare_source_identity(source, oracle, &mut report);
    compare_files(oracle, scan, &mut report);
    compare_retains(source, oracle, scan, &mut report);
    report
}

fn compare_source_identity(source: &DocsSource, oracle: &OracleDump, report: &mut ShadowReport) {
    let tree = &oracle.provenance.tree;
    if source.tag != tree.source_tag {
        report.note(Divergence::new(
            DivergenceKind::SourceTag,
            format!(
                "rust tag {:?} != oracle tag {:?}",
                source.tag, tree.source_tag
            ),
        ));
    }
    if source.bank_id != tree.bank_id {
        report.note(Divergence::new(
            DivergenceKind::SourceBank,
            format!(
                "rust bank {:?} != oracle bank {:?}",
                source.bank_id, tree.bank_id
            ),
        ));
    }
    if !same_path(&source.root, Path::new(&tree.root)) {
        report.note(Divergence::new(
            DivergenceKind::SourceRoot,
            format!(
                "rust root {:?} != oracle root {:?}",
                source.root.display(),
                tree.root
            ),
        ));
    }
    if source.docs_include != tree.docs_include {
        report.note(Divergence::new(
            DivergenceKind::SourcePatterns,
            format!(
                "include patterns differ: rust {:?} != oracle {:?}",
                source.docs_include, tree.docs_include
            ),
        ));
    }
    if source.docs_exclude != tree.docs_exclude {
        report.note(Divergence::new(
            DivergenceKind::SourcePatterns,
            format!(
                "exclude patterns differ: rust {:?} != oracle {:?}",
                source.docs_exclude, tree.docs_exclude
            ),
        ));
    }
}

fn same_path(left: &Path, right: &Path) -> bool {
    match (std::fs::canonicalize(left), std::fs::canonicalize(right)) {
        (Ok(left), Ok(right)) => left == right,
        _ => false,
    }
}

fn compare_files(oracle: &OracleDump, scan: &[ScannedFile], report: &mut ShadowReport) {
    let oracle_files: BTreeMap<&str, &OracleFile> = oracle
        .files
        .iter()
        .map(|file| (file.relative_path.as_str(), file))
        .collect();
    let rust_files: BTreeMap<&str, &ScannedFile> = scan
        .iter()
        .map(|file| (file.relative_path.as_str(), file))
        .collect();

    report.files.oracle_files = oracle_files.len();
    report.files.rust_files = rust_files.len();

    for path in oracle_files.keys() {
        if !rust_files.contains_key(path) {
            report.note(
                Divergence::new(
                    DivergenceKind::MissingFile,
                    "oracle file is absent from scan",
                )
                .at(path),
            );
        }
    }
    for path in rust_files.keys() {
        if !oracle_files.contains_key(path) {
            report.note(
                Divergence::new(DivergenceKind::ExtraFile, "scan file is absent from oracle")
                    .at(path),
            );
        }
    }

    for (path, oracle_file) in &oracle_files {
        let Some(rust_file) = rust_files.get(path) else {
            continue;
        };
        report.files.matched += 1;
        if oracle_file.content_sha256 != rust_file.content_sha256.to_hex() {
            report.note(
                Divergence::new(
                    DivergenceKind::FileContent,
                    format!(
                        "decoded content sha256 {} != {}",
                        oracle_file.content_sha256,
                        rust_file.content_sha256.to_hex()
                    ),
                )
                .at(path),
            );
        }
        if oracle_file.size_bytes != rust_file.size_bytes {
            report.note(
                Divergence::new(
                    DivergenceKind::FileSize,
                    format!(
                        "size {} != {}",
                        oracle_file.size_bytes, rust_file.size_bytes
                    ),
                )
                .at(path),
            );
        }
        let rust_timestamp = rust_file
            .calls
            .first()
            .and_then(|call| call.timestamp.clone());
        if oracle_file.timestamp != rust_timestamp && !rust_file.calls.is_empty() {
            report.note(
                Divergence::new(
                    DivergenceKind::FileTimestamp,
                    format!(
                        "file timestamp {:?} != first retain timestamp {:?}",
                        oracle_file.timestamp, rust_timestamp
                    ),
                )
                .at(path),
            );
        }
    }
}

fn compare_retains(
    source: &DocsSource,
    oracle: &OracleDump,
    scan: &[ScannedFile],
    report: &mut ShadowReport,
) {
    let mut oracle_by_id: BTreeMap<&str, Vec<&OracleRetain>> = BTreeMap::new();
    let mut oracle_by_path: BTreeMap<&str, Vec<&OracleRetain>> = BTreeMap::new();
    for retain in &oracle.retains {
        oracle_by_id
            .entry(retain.document_id.as_str())
            .or_default()
            .push(retain);
        oracle_by_path
            .entry(retain.relative_path.as_str())
            .or_default()
            .push(retain);
    }
    for calls in oracle_by_id.values_mut() {
        calls.sort_by_key(|retain| retain.sequence);
        if calls.len() > 1 {
            report.retains.duplicate_ids += 1;
        }
    }
    for calls in oracle_by_path.values_mut() {
        calls.sort_by_key(|retain| retain.sequence);
    }

    let mut rust_by_id: BTreeMap<&str, Vec<(&str, &crate::document::RetainCall)>> = BTreeMap::new();
    let mut rust_by_path: BTreeMap<&str, Vec<&crate::document::RetainCall>> = BTreeMap::new();
    for file in scan {
        for call in &file.calls {
            rust_by_id
                .entry(call.document_id.as_str())
                .or_default()
                .push((file.relative_path.as_str(), call));
            rust_by_path
                .entry(file.relative_path.as_str())
                .or_default()
                .push(call);
        }
    }

    report.retains.oracle_retains = oracle.retains.len();
    report.retains.rust_retains = rust_by_id.values().map(Vec::len).sum();

    for document_id in oracle_by_id.keys() {
        if !rust_by_id.contains_key(document_id) {
            let path = oracle_by_id[*document_id]
                .first()
                .map(|retain| retain.relative_path.clone())
                .unwrap_or_default();
            report.note(
                Divergence::new(
                    DivergenceKind::MissingRetain,
                    "oracle retain is absent from the Rust plan",
                )
                .for_document(&path, document_id),
            );
        }
    }
    for document_id in rust_by_id.keys() {
        if !oracle_by_id.contains_key(document_id) {
            let path = rust_by_id[*document_id]
                .first()
                .map(|(path, _)| (*path).to_string())
                .unwrap_or_default();
            report.note(
                Divergence::new(
                    DivergenceKind::ExtraRetain,
                    "Rust retain is absent from the oracle",
                )
                .for_document(&path, document_id),
            );
        }
    }

    for (document_id, oracle_calls) in &oracle_by_id {
        let Some(rust_calls) = rust_by_id.get(document_id) else {
            continue;
        };
        if oracle_calls.len() != rust_calls.len() {
            let kind = if oracle_calls.len() > rust_calls.len() {
                DivergenceKind::MissingRetain
            } else {
                DivergenceKind::ExtraRetain
            };
            report.note(
                Divergence::new(
                    kind,
                    format!(
                        "occurrence count {} != {}",
                        oracle_calls.len(),
                        rust_calls.len()
                    ),
                )
                .for_document(&oracle_calls[0].relative_path, document_id),
            );
        }
        for (oracle_call, (rust_path, rust_call)) in oracle_calls.iter().zip(rust_calls.iter()) {
            report.retains.matched += 1;
            if oracle_call.relative_path != *rust_path {
                report.note(
                    Divergence::new(
                        DivergenceKind::RetainPath,
                        format!(
                            "oracle path {:?} != Rust path {:?}",
                            oracle_call.relative_path, rust_path
                        ),
                    )
                    .for_document(&oracle_call.relative_path, document_id),
                );
            }
            if oracle_call.chunk_key != rust_call.chunk_key {
                report.note(
                    Divergence::new(
                        DivergenceKind::ChunkKey,
                        format!(
                            "oracle chunk key {:?} != Rust chunk key {:?}",
                            oracle_call.chunk_key, rust_call.chunk_key
                        ),
                    )
                    .for_document(&oracle_call.relative_path, document_id),
                );
            }
            if oracle_call.content != rust_call.content {
                report.note(
                    Divergence::new(
                        DivergenceKind::Content,
                        format!(
                            "content differs (oracle {} bytes, Rust {} bytes)",
                            oracle_call.content.len(),
                            rust_call.content.len()
                        ),
                    )
                    .for_document(&oracle_call.relative_path, document_id),
                );
            } else if oracle_call.content_sha256 != rust_call.content_sha256.to_hex() {
                report.note(
                    Divergence::new(
                        DivergenceKind::Content,
                        "content bytes match but sha256 differs",
                    )
                    .for_document(&oracle_call.relative_path, document_id),
                );
            }
            if oracle_call.metadata != rust_call.metadata {
                report.note(
                    Divergence::new(
                        DivergenceKind::Metadata,
                        format!(
                            "metadata differs: {}",
                            metadata_difference(&oracle_call.metadata, &rust_call.metadata)
                        ),
                    )
                    .for_document(&oracle_call.relative_path, document_id),
                );
            }
            if oracle_call.tags != rust_call.tags {
                report.note(
                    Divergence::new(
                        DivergenceKind::Tags,
                        format!("tags {:?} != {:?}", oracle_call.tags, rust_call.tags),
                    )
                    .for_document(&oracle_call.relative_path, document_id),
                );
            }
            if oracle_call.timestamp != rust_call.timestamp {
                report.note(
                    Divergence::new(
                        DivergenceKind::Timestamp,
                        format!(
                            "timestamp {:?} != {:?}",
                            oracle_call.timestamp, rust_call.timestamp
                        ),
                    )
                    .for_document(&oracle_call.relative_path, document_id),
                );
            }
            if oracle_call.bank_id != source.bank_id {
                report.note(
                    Divergence::new(
                        DivergenceKind::Bank,
                        format!(
                            "oracle bank {:?} != configured bank {:?}",
                            oracle_call.bank_id, source.bank_id
                        ),
                    )
                    .for_document(&oracle_call.relative_path, document_id),
                );
            }
        }
    }

    for (path, oracle_calls) in &oracle_by_path {
        let Some(rust_calls) = rust_by_path.get(path) else {
            continue;
        };
        let oracle_order: Vec<&str> = oracle_calls
            .iter()
            .map(|retain| retain.document_id.as_str())
            .collect();
        let rust_order: Vec<&str> = rust_calls
            .iter()
            .map(|call| call.document_id.as_str())
            .collect();
        if oracle_order != rust_order {
            report.note(
                Divergence::new(
                    DivergenceKind::Order,
                    format!("chunk order differs: oracle {oracle_order:?} != Rust {rust_order:?}"),
                )
                .at(path),
            );
        }
    }
}

fn metadata_difference(
    oracle: &BTreeMap<String, String>,
    rust: &BTreeMap<String, String>,
) -> String {
    let mut differences = Vec::new();
    for key in oracle.keys().chain(rust.keys()) {
        let oracle_value = oracle.get(key);
        let rust_value = rust.get(key);
        if oracle_value != rust_value {
            differences.push(format!("{key}: {oracle_value:?} != {rust_value:?}"));
        }
    }
    differences.sort();
    differences.dedup();
    differences.join("; ")
}

/// Verify the ledger's normalized retain set: the oracle may emit duplicate
/// document ids for repeated headings, while the ledger keeps only the last
/// occurrence. Every other difference remains unexpected.
pub fn compare_ledger(oracle: &OracleDump, reconciliation: &Reconciliation) -> ShadowReport {
    let mut report = ShadowReport::default();
    let mut oracle_last: BTreeMap<&str, &OracleRetain> = BTreeMap::new();
    for retain in &oracle.retains {
        oracle_last.insert(retain.document_id.as_str(), retain);
    }
    let mut rust: BTreeMap<&str, Vec<(&str, &crate::document::RetainCall)>> = BTreeMap::new();
    let mut total_rust_calls = 0;
    for file in &reconciliation.retain {
        for call in &file.calls {
            total_rust_calls += 1;
            rust.entry(call.document_id.as_str())
                .or_default()
                .push((file.relative_path.as_str(), call));
        }
    }

    report.retains.oracle_retains = oracle.retains.len();
    report.retains.rust_retains = total_rust_calls;

    let mut duplicate_planner_ids: BTreeSet<&str> = BTreeSet::new();
    for (document_id, calls) in &rust {
        if calls.len() > 1 {
            duplicate_planner_ids.insert(document_id);
            let (path, _) = calls[0];
            report.note(
                Divergence::new(
                    DivergenceKind::ExtraRetain,
                    format!(
                        "reconciled plan emitted {} retains for one document id",
                        calls.len()
                    ),
                )
                .for_document(path, document_id),
            );
        }
    }

    for (document_id, oracle_call) in &oracle_last {
        let Some(calls) = rust.get(document_id) else {
            report.note(
                Divergence::new(
                    DivergenceKind::MissingRetain,
                    "oracle document id is absent from the reconciled plan",
                )
                .for_document(&oracle_call.relative_path, document_id),
            );
            continue;
        };
        let (path, rust_call) = *calls.last().expect("at least one retain");
        report.retains.matched += 1;
        if oracle_call.content != rust_call.content {
            report.note(
                Divergence::new(
                    DivergenceKind::Content,
                    "last oracle occurrence differs from the reconciled retain",
                )
                .for_document(path, document_id),
            );
        }
        if oracle_call.metadata != rust_call.metadata {
            report.note(
                Divergence::new(
                    DivergenceKind::Metadata,
                    "last oracle occurrence metadata differs from the reconciled retain",
                )
                .for_document(path, document_id),
            );
        }
        if oracle_call.tags != rust_call.tags {
            report.note(
                Divergence::new(
                    DivergenceKind::Tags,
                    "last oracle occurrence tags differ from the reconciled retain",
                )
                .for_document(path, document_id),
            );
        }
        if oracle_call.timestamp != rust_call.timestamp {
            report.note(
                Divergence::new(
                    DivergenceKind::Timestamp,
                    "last oracle occurrence timestamp differs from the reconciled retain",
                )
                .for_document(path, document_id),
            );
        }
    }
    for (document_id, calls) in &rust {
        if !oracle_last.contains_key(document_id) {
            let (path, _) = calls[0];
            report.note(
                Divergence::new(
                    DivergenceKind::ExtraRetain,
                    "reconciled document id is absent from the oracle",
                )
                .for_document(path, document_id),
            );
        }
    }

    // Count duplicate ids explicitly so the report can show the documented
    // repeated-heading collapse instead of hiding it. A duplicate is only
    // expected when every occurrence belongs to the same file and the same
    // non-empty heading key; anything else is an id collision.
    let mut occurrences: BTreeMap<&str, Vec<&OracleRetain>> = BTreeMap::new();
    for retain in &oracle.retains {
        occurrences
            .entry(retain.document_id.as_str())
            .or_default()
            .push(retain);
    }
    for (document_id, calls) in &occurrences {
        if calls.len() < 2 || duplicate_planner_ids.contains(document_id) {
            continue;
        }
        let path = &calls[0].relative_path;
        let key = &calls[0].chunk_key;
        let same_section = calls
            .iter()
            .all(|call| &call.relative_path == path && &call.chunk_key == key);
        if same_section && !key.is_empty() {
            let last = oracle_last
                .get(document_id)
                .expect("last occurrence recorded");
            report.note(
                Divergence::new(
                    DivergenceKind::DuplicateCollapse,
                    format!(
                        "oracle emitted {} retains; ledger keeps the last occurrence ({})",
                        calls.len(),
                        last.content_sha256
                    ),
                )
                .for_document(path, document_id),
            );
        } else if same_section {
            report.note(
                Divergence::new(
                    DivergenceKind::ChunkKey,
                    "document id repeats for a bare/positional key; only repeated headings may collapse",
                )
                .for_document(path, document_id),
            );
        } else {
            report.note(
                Divergence::new(
                    DivergenceKind::RetainPath,
                    "document id is emitted by more than one file or section",
                )
                .for_document(path, document_id),
            );
        }
    }
    report.retains.duplicate_ids = occurrences.values().filter(|count| count.len() > 1).count();
    report
}

/// A scan that produces one Hindsight document id from two files must never be
/// treated as a benign duplicate-heading collapse.
pub fn conflict_divergences(reconciliation: &Reconciliation) -> Vec<Divergence> {
    reconciliation
        .conflicts
        .iter()
        .map(|conflict| {
            Divergence::new(
                DivergenceKind::Conflict,
                format!(
                    "document id is produced by multiple files: {:?}",
                    conflict.paths
                ),
            )
            .for_document(
                conflict.paths.first().map(String::as_str).unwrap_or(""),
                &conflict.hindsight_document_id,
            )
        })
        .collect()
}

pub fn merge_conflicts(report: &mut ShadowReport, reconciliation: &Reconciliation) {
    for divergence in conflict_divergences(reconciliation) {
        report.note(divergence);
    }
}

/// Build the ledger snapshot a prior successful apply would have stored.
pub fn manifest_from_scan(scan: &[ScannedFile], revision: i64) -> Manifest {
    let mut manifest = Manifest {
        revision,
        ..Manifest::default()
    };
    for file in scan {
        manifest.documents.insert(
            file.relative_path.clone(),
            ManifestDocument {
                relative_path: file.relative_path.clone(),
                content_sha256: file.content_sha256,
                size_bytes: file.size_bytes,
                layout_version: LAYOUT_VERSION,
                state: RecordState::Active,
            },
        );
        // Keep the last occurrence per document id, matching `reconcile` and
        // Hindsight's final-state semantics, while preserving the forward order
        // of the surviving chunks.
        let mut last_index = BTreeMap::new();
        for (index, call) in file.calls.iter().enumerate() {
            last_index.insert(call.document_id.as_str(), index);
        }
        let mut ordinal = 0;
        for (index, call) in file.calls.iter().enumerate() {
            if last_index.get(call.document_id.as_str()) != Some(&index) {
                continue;
            }
            manifest.chunks.insert(
                call.document_id.clone(),
                ManifestChunk {
                    document_path: file.relative_path.clone(),
                    hindsight_document_id: call.document_id.clone(),
                    chunk_key: call.chunk_key.clone(),
                    content_sha256: call.content_sha256,
                    ordinal,
                    generation: 1,
                    state: RecordState::Active,
                },
            );
            ordinal += 1;
        }
    }
    manifest
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExpectedDeleteReason {
    FileRemoved,
    ChunkRemoved,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ExpectedDelete {
    pub document_id: String,
    pub relative_path: String,
    pub chunk_key: String,
    pub content_sha256: String,
    pub reason: ExpectedDeleteReason,
}

/// Derive the deletes the native ledger must emit to converge on the second
/// tree. The Python flow has no source delete; the expectation comes from the
/// oracle's two measured retain snapshots, not from planner output.
pub fn expected_deletes(
    before: &OracleDump,
    after: &OracleDump,
) -> BTreeMap<String, ExpectedDelete> {
    let after_ids: BTreeSet<&str> = after
        .retains
        .iter()
        .map(|retain| retain.document_id.as_str())
        .collect();
    let after_files: BTreeSet<&str> = after
        .files
        .iter()
        .map(|file| file.relative_path.as_str())
        .collect();
    let after_paths_with_retains: BTreeSet<&str> = after
        .retains
        .iter()
        .map(|retain| retain.relative_path.as_str())
        .collect();
    let mut deletes = BTreeMap::new();
    for retain in &before.retains {
        if after_ids.contains(retain.document_id.as_str()) {
            continue;
        }
        let reason = if after_files.contains(retain.relative_path.as_str()) {
            // A file that became whitespace-only is skipped by both flows, so
            // its old chunks intentionally stay in place; do not expect a
            // delete for that transition.
            if !after_paths_with_retains.contains(retain.relative_path.as_str()) {
                continue;
            }
            ExpectedDeleteReason::ChunkRemoved
        } else {
            ExpectedDeleteReason::FileRemoved
        };
        deletes.insert(
            retain.document_id.clone(),
            ExpectedDelete {
                document_id: retain.document_id.clone(),
                relative_path: retain.relative_path.clone(),
                chunk_key: retain.chunk_key.clone(),
                content_sha256: retain.content_sha256.clone(),
                reason,
            },
        );
    }
    deletes
}

/// Compare the ledger's planned deletes with the oracle-derived expectation.
pub fn compare_deletes(
    expected: &BTreeMap<String, ExpectedDelete>,
    reconciliation: &Reconciliation,
) -> (DeleteParity, Vec<Divergence>) {
    let mut parity = DeleteParity {
        expected: expected.len(),
        emitted: reconciliation.deletes.len(),
        ..DeleteParity::default()
    };
    let mut divergences = Vec::new();
    let mut emitted: BTreeMap<&str, &DeleteChunk> = BTreeMap::new();
    for delete in &reconciliation.deletes {
        if emitted
            .insert(delete.hindsight_document_id.as_str(), delete)
            .is_some()
        {
            divergences.push(
                Divergence::new(
                    DivergenceKind::ExtraDelete,
                    "reconciled plan emitted more than one delete for one document id",
                )
                .for_document(&delete.relative_path, &delete.hindsight_document_id),
            );
        }
    }

    for (document_id, expected_delete) in expected {
        let Some(delete) = emitted.get(document_id.as_str()) else {
            divergences.push(
                Divergence::new(
                    DivergenceKind::MissingDelete,
                    format!(
                        "expected {:?} delete was not emitted",
                        expected_delete.reason
                    ),
                )
                .for_document(&expected_delete.relative_path, document_id),
            );
            continue;
        };
        let expected_reason = match expected_delete.reason {
            ExpectedDeleteReason::FileRemoved => crate::reconcile::DeleteReason::FileRemoved,
            ExpectedDeleteReason::ChunkRemoved => crate::reconcile::DeleteReason::ChunkRemoved,
        };
        if delete.reason != expected_reason {
            divergences.push(
                Divergence::new(
                    DivergenceKind::DeleteReason,
                    format!(
                        "reason {:?} != expected {:?}",
                        delete.reason, expected_reason
                    ),
                )
                .for_document(&expected_delete.relative_path, document_id),
            );
        }
        if delete.content_sha256.to_hex() != expected_delete.content_sha256 {
            divergences.push(
                Divergence::new(
                    DivergenceKind::DeleteContent,
                    format!(
                        "delete hash {} != oracle {}",
                        delete.content_sha256.to_hex(),
                        expected_delete.content_sha256
                    ),
                )
                .for_document(&expected_delete.relative_path, document_id),
            );
        }
        if delete.chunk_key != expected_delete.chunk_key {
            divergences.push(
                Divergence::new(
                    DivergenceKind::ChunkKey,
                    format!(
                        "delete chunk key {:?} != oracle {:?}",
                        delete.chunk_key, expected_delete.chunk_key
                    ),
                )
                .for_document(&expected_delete.relative_path, document_id),
            );
        }
        if delete.relative_path != expected_delete.relative_path {
            divergences.push(
                Divergence::new(
                    DivergenceKind::RetainPath,
                    format!(
                        "delete path {:?} != oracle {:?}",
                        delete.relative_path, expected_delete.relative_path
                    ),
                )
                .for_document(&expected_delete.relative_path, document_id),
            );
        }
        parity.matched += 1;
    }
    for (document_id, delete) in &emitted {
        if !expected.contains_key(*document_id) {
            divergences.push(
                Divergence::new(
                    DivergenceKind::ExtraDelete,
                    "delete is absent from the oracle-derived expectation",
                )
                .for_document(&delete.relative_path, document_id),
            );
        }
    }
    (parity, divergences)
}

/// Merge a delete comparison into an existing report.
pub fn merge_deletes(
    report: &mut ShadowReport,
    parity: DeleteParity,
    divergences: Vec<Divergence>,
) {
    report.deletes = parity;
    for divergence in divergences {
        report.note(divergence);
    }
}

/// Parse the oracle JSON emitted by `fixtures/docs_shadow_oracle.py`.
pub fn parse_oracle(json: &str) -> Result<OracleDump, serde_json::Error> {
    serde_json::from_str(json)
}

/// Reject a dump whose toolchain or flow provenance is missing; callers record
/// the rest of the provenance in the measured report.
pub fn validate_oracle(oracle: &OracleDump) -> Result<(), String> {
    if oracle.provenance.python.is_empty() || !oracle.provenance.python.starts_with("3.14") {
        return Err(format!(
            "oracle was captured with Python {:?}; the deployed flow is 3.14.x",
            oracle.provenance.python
        ));
    }
    if oracle.provenance.flow_source_commit.is_empty()
        || oracle.provenance.flow_source_commit == "unknown"
    {
        return Err("oracle provenance has no flow source commit".to_string());
    }
    Ok(())
}

/// Convenience for tests and reports: a content hash as lowercase hex.
pub fn hash_hex(bytes: &[u8]) -> String {
    ContentHash::of(bytes).to_hex()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::document::RetainCall;
    use crate::reconcile::reconcile;

    fn root() -> String {
        std::env::temp_dir().display().to_string()
    }

    fn source() -> DocsSource {
        DocsSource {
            project: "engram".to_string(),
            tag: "engram-docs".to_string(),
            root: root().into(),
            bank_id: "engram-docs".to_string(),
            docs_include: vec!["**/*.md".to_string()],
            docs_exclude: Vec::new(),
        }
    }

    fn metadata(repo: &str) -> BTreeMap<String, String> {
        BTreeMap::from([
            ("source".to_string(), "cocoindex".to_string()),
            ("repo".to_string(), repo.to_string()),
        ])
    }

    fn retain(
        path: &str,
        document_id: &str,
        chunk_key: &str,
        content: &str,
        timestamp: Option<&str>,
    ) -> RetainCall {
        let section = path.split('/').next().unwrap_or("root");
        RetainCall {
            document_id: document_id.to_string(),
            chunk_key: chunk_key.to_string(),
            content: content.to_string(),
            content_sha256: ContentHash::of(content.as_bytes()),
            metadata: metadata("engram-docs"),
            tags: vec![section.to_string(), "engram-docs".to_string()],
            timestamp: timestamp.map(str::to_string),
        }
    }

    fn scanned(path: &str, content: &str, calls: Vec<RetainCall>) -> ScannedFile {
        ScannedFile {
            relative_path: path.to_string(),
            content_sha256: ContentHash::of(content.as_bytes()),
            size_bytes: content.len() as i64,
            calls,
        }
    }

    fn oracle_file(path: &str, content: &str, timestamp: Option<&str>) -> OracleFile {
        OracleFile {
            relative_path: path.to_string(),
            base_id: format!("engram-docs--{}", path.trim_end_matches(".md")),
            content_sha256: ContentHash::of(content.as_bytes()).to_hex(),
            size_bytes: content.len() as i64,
            timestamp: timestamp.map(str::to_string),
        }
    }

    fn oracle_retain(
        sequence: u64,
        path: &str,
        document_id: &str,
        chunk_key: &str,
        content: &str,
        timestamp: Option<&str>,
    ) -> OracleRetain {
        let section = path.split('/').next().unwrap_or("root");
        OracleRetain {
            sequence,
            bank_id: "engram-docs".to_string(),
            document_id: document_id.to_string(),
            relative_path: path.to_string(),
            chunk_key: chunk_key.to_string(),
            content: content.to_string(),
            content_sha256: ContentHash::of(content.as_bytes()).to_hex(),
            metadata: metadata("engram-docs"),
            tags: vec![section.to_string(), "engram-docs".to_string()],
            timestamp: timestamp.map(str::to_string),
        }
    }

    fn oracle(files: Vec<OracleFile>, retains: Vec<OracleRetain>) -> OracleDump {
        OracleDump {
            provenance: OracleProvenance {
                python: "3.14.6".to_string(),
                cocoindex: "1.0.26".to_string(),
                sklearn: "1.9.1".to_string(),
                networkx: "3.7".to_string(),
                flow_source_commit: "abc123".to_string(),
                flow_source_dirty: false,
                flow_source_sha256: BTreeMap::new(),
                generated_at: "2026-11-26T00:00:00Z".to_string(),
                tree: OracleTree {
                    root: root(),
                    source_tag: "engram-docs".to_string(),
                    bank_id: "engram-docs".to_string(),
                    docs_include: vec!["**/*.md".to_string()],
                    docs_exclude: Vec::new(),
                    files: files.len(),
                    bytes: files.iter().map(|file| file.size_bytes as u64).sum(),
                    sha256: "tree".to_string(),
                },
            },
            files,
            retains,
        }
    }

    fn kinds(report: &ShadowReport) -> Vec<DivergenceKind> {
        report
            .divergences
            .iter()
            .map(|divergence| divergence.kind)
            .collect()
    }

    #[test]
    fn matching_retains_have_no_divergences() {
        let content = "# A\n\nbody\n";
        let call = retain(
            "a.md",
            "engram-docs--a",
            "",
            content,
            Some("2026-01-01T00:00:00"),
        );
        let scan = vec![scanned("a.md", content, vec![call])];
        let dump = oracle(
            vec![oracle_file("a.md", content, Some("2026-01-01T00:00:00"))],
            vec![oracle_retain(
                1,
                "a.md",
                "engram-docs--a",
                "",
                content,
                Some("2026-01-01T00:00:00"),
            )],
        );

        let report = compare(&source(), &dump, &scan);
        assert_eq!(report.unexpected, 0, "{:?}", report.divergences);
        assert_eq!(report.files.matched, 1);
        assert_eq!(report.retains.matched, 1);
        assert_eq!(report.retains.duplicate_ids, 0);
    }

    #[test]
    fn retain_field_mismatches_are_all_reported() {
        let call = retain(
            "b.md",
            "engram-docs--a--k",
            "j",
            "rust content",
            Some("2026-01-02T00:00:00"),
        );
        let scan = vec![scanned("b.md", "rust content", vec![call])];
        let mut oracle_retain = oracle_retain(
            1,
            "a.md",
            "engram-docs--a--k",
            "k",
            "oracle content",
            Some("2026-01-01T00:00:00"),
        );
        oracle_retain.metadata = metadata("other-repo");
        oracle_retain.tags = vec!["other".to_string(), "engram-docs".to_string()];
        let dump = oracle(
            vec![oracle_file(
                "b.md",
                "oracle content",
                Some("2026-01-01T00:00:00"),
            )],
            vec![oracle_retain],
        );

        let report = compare(&source(), &dump, &scan);
        let kinds = kinds(&report);
        for expected in [
            DivergenceKind::RetainPath,
            DivergenceKind::ChunkKey,
            DivergenceKind::Content,
            DivergenceKind::Metadata,
            DivergenceKind::Tags,
            DivergenceKind::Timestamp,
            DivergenceKind::FileContent,
        ] {
            assert!(kinds.contains(&expected), "missing {expected:?}: {kinds:?}");
        }
    }

    #[test]
    fn bank_and_source_identity_mismatches_are_reported() {
        let content = "# A\n\nbody\n";
        let call = retain("a.md", "engram-docs--a", "", content, None);
        let scan = vec![scanned("a.md", content, vec![call])];
        let mut dump = oracle(
            vec![oracle_file("a.md", content, None)],
            vec![oracle_retain(
                1,
                "a.md",
                "engram-docs--a",
                "",
                content,
                None,
            )],
        );
        dump.provenance.tree.source_tag = "other-tag".to_string();
        dump.provenance.tree.bank_id = "other-bank".to_string();
        dump.provenance.tree.root = "/tmp/other-root".to_string();
        dump.provenance.tree.docs_include = vec!["docs/**/*.md".to_string()];

        let report = compare(&source(), &dump, &scan);
        let kinds = kinds(&report);
        for expected in [
            DivergenceKind::SourceTag,
            DivergenceKind::SourceBank,
            DivergenceKind::SourceRoot,
            DivergenceKind::SourcePatterns,
        ] {
            assert!(kinds.contains(&expected), "missing {expected:?}: {kinds:?}");
        }
    }

    #[test]
    fn file_set_and_file_facts_are_compared() {
        let content = "# A\n\nbody\n";
        let call = retain("a.md", "engram-docs--a", "", content, Some("t2"));
        let scan = vec![scanned("a.md", "different", vec![call])];
        let dump = oracle(
            vec![
                oracle_file("a.md", content, Some("t1")),
                oracle_file("missing.md", "x", None),
            ],
            vec![oracle_retain(
                1,
                "a.md",
                "engram-docs--a",
                "",
                content,
                Some("t2"),
            )],
        );

        let report = compare(&source(), &dump, &scan);
        let found = kinds(&report);
        for expected in [
            DivergenceKind::MissingFile,
            DivergenceKind::FileContent,
            DivergenceKind::FileSize,
            DivergenceKind::FileTimestamp,
        ] {
            assert!(found.contains(&expected), "missing {expected:?}: {found:?}");
        }

        let dump = oracle(vec![oracle_file("other.md", "x", None)], Vec::new());
        let scan = vec![scanned("a.md", content, Vec::new())];
        let report = compare(&source(), &dump, &scan);
        assert!(kinds(&report).contains(&DivergenceKind::ExtraFile));
    }

    #[test]
    fn per_document_chunk_order_is_compared() {
        let first = "# A\n\na\n";
        let second = "## B\n\nb\n";
        let calls = vec![
            retain("a.md", "engram-docs--a", "", first, None),
            retain("a.md", "engram-docs--a--b", "b", second, None),
        ];
        let scan = vec![scanned("a.md", first, calls)];
        let dump = oracle(
            vec![oracle_file("a.md", first, None)],
            vec![
                oracle_retain(1, "a.md", "engram-docs--a--b", "b", second, None),
                oracle_retain(2, "a.md", "engram-docs--a", "", first, None),
            ],
        );

        let report = compare(&source(), &dump, &scan);
        assert!(kinds(&report).contains(&DivergenceKind::Order));
    }

    #[test]
    fn duplicate_headings_collapse_to_the_last_occurrence() {
        let one = "## Notes\n\nfirst\n";
        let two = "## Notes\n\nsecond\n";
        let calls = vec![
            retain("a.md", "engram-docs--a--notes", "notes", one, None),
            retain("a.md", "engram-docs--a--notes", "notes", two, None),
        ];
        let scan = vec![scanned("a.md", two, calls)];
        let dump = oracle(
            vec![oracle_file("a.md", two, None)],
            vec![
                oracle_retain(1, "a.md", "engram-docs--a--notes", "notes", one, None),
                oracle_retain(2, "a.md", "engram-docs--a--notes", "notes", two, None),
            ],
        );

        let raw = compare(&source(), &dump, &scan);
        assert_eq!(raw.unexpected, 0, "{:?}", raw.divergences);
        assert_eq!(raw.retains.duplicate_ids, 1);

        let reconciliation = reconcile(&Manifest::default(), &scan, LAYOUT_VERSION);
        let ledger = compare_ledger(&dump, &reconciliation);
        assert_eq!(ledger.unexpected, 0, "{:?}", ledger.divergences);
        assert_eq!(ledger.retains.duplicate_ids, 1);
        assert_eq!(
            ledger.divergences[0].kind,
            DivergenceKind::DuplicateCollapse
        );
        assert_eq!(reconciliation.retain[0].calls.len(), 1);
        assert_eq!(reconciliation.retain[0].calls[0].content, two);
    }

    #[test]
    fn manifest_from_scan_keeps_last_duplicate_in_forward_order() {
        let first = retain("a.md", "engram-docs--a", "", "one", None);
        let middle = retain("a.md", "engram-docs--a--b", "b", "middle", None);
        let last = retain("a.md", "engram-docs--a", "", "two", None);
        let scan = vec![scanned("a.md", "two", vec![first, middle, last])];

        let manifest = manifest_from_scan(&scan, 7);
        assert_eq!(manifest.revision, 7);
        let a = &manifest.chunks["engram-docs--a"];
        assert_eq!(a.content_sha256, ContentHash::of(b"two"));
        assert_eq!(a.ordinal, 1);
        let b = &manifest.chunks["engram-docs--a--b"];
        assert_eq!(b.ordinal, 0);
    }

    fn delete_scenario() -> (OracleDump, OracleDump, Manifest, Vec<ScannedFile>) {
        let a0 = "# A\n\nkeep\n";
        let a1 = "## B\n\nremoved\n";
        let b0 = "# B\n\nwhole file\n";
        let before_scan = vec![
            scanned(
                "a.md",
                a0,
                vec![
                    retain("a.md", "engram-docs--a", "", a0, None),
                    retain("a.md", "engram-docs--a--b", "b", a1, None),
                ],
            ),
            scanned(
                "b.md",
                b0,
                vec![retain("b.md", "engram-docs--b", "", b0, None)],
            ),
        ];
        let after_scan = vec![scanned(
            "a.md",
            a0,
            vec![retain("a.md", "engram-docs--a", "", a0, None)],
        )];
        let before = oracle(
            vec![oracle_file("a.md", a0, None), oracle_file("b.md", b0, None)],
            vec![
                oracle_retain(1, "a.md", "engram-docs--a", "", a0, None),
                oracle_retain(2, "a.md", "engram-docs--a--b", "b", a1, None),
                oracle_retain(3, "b.md", "engram-docs--b", "", b0, None),
            ],
        );
        let after = oracle(
            vec![oracle_file("a.md", a0, None)],
            vec![oracle_retain(1, "a.md", "engram-docs--a", "", a0, None)],
        );
        let manifest = manifest_from_scan(&before_scan, 1);
        (before, after, manifest, after_scan)
    }

    #[test]
    fn deletes_match_the_oracle_derived_expectation() {
        let (before, after, manifest, after_scan) = delete_scenario();
        let expected = expected_deletes(&before, &after);
        let reconciliation = reconcile(&manifest, &after_scan, LAYOUT_VERSION);

        assert_eq!(expected.len(), 2);
        assert_eq!(
            expected["engram-docs--a--b"].reason,
            ExpectedDeleteReason::ChunkRemoved
        );
        assert_eq!(
            expected["engram-docs--b"].reason,
            ExpectedDeleteReason::FileRemoved
        );

        let (parity, divergences) = compare_deletes(&expected, &reconciliation);
        assert!(divergences.is_empty(), "{divergences:?}");
        assert_eq!(parity.expected, 2);
        assert_eq!(parity.emitted, 2);
        assert_eq!(parity.matched, 2);
    }

    #[test]
    fn missing_and_extra_deletes_are_reported() {
        let (before, after, manifest, after_scan) = delete_scenario();
        let expected = expected_deletes(&before, &after);
        let mut reconciliation = reconcile(&manifest, &after_scan, LAYOUT_VERSION);
        reconciliation.deletes.pop();
        reconciliation.deletes.push(DeleteChunk {
            hindsight_document_id: "engram-docs--ghost".to_string(),
            chunk_key: "ghost".to_string(),
            content_sha256: ContentHash::of(b"ghost"),
            generation: 1,
            relative_path: "ghost.md".to_string(),
            reason: crate::reconcile::DeleteReason::FileRemoved,
        });

        let (_, divergences) = compare_deletes(&expected, &reconciliation);
        let kinds: Vec<_> = divergences.iter().map(|item| item.kind).collect();
        assert!(kinds.contains(&DivergenceKind::MissingDelete));
        assert!(kinds.contains(&DivergenceKind::ExtraDelete));
    }

    #[test]
    fn duplicate_delete_operations_are_reported() {
        let (before, after, manifest, after_scan) = delete_scenario();
        let expected = expected_deletes(&before, &after);
        let mut reconciliation = reconcile(&manifest, &after_scan, LAYOUT_VERSION);
        reconciliation
            .deletes
            .push(reconciliation.deletes[0].clone());

        let (parity, divergences) = compare_deletes(&expected, &reconciliation);
        assert_eq!(parity.emitted, 3);
        assert!(divergences
            .iter()
            .any(|item| item.kind == DivergenceKind::ExtraDelete));
    }

    #[test]
    fn delete_path_mismatches_are_reported() {
        let (before, after, manifest, after_scan) = delete_scenario();
        let expected = expected_deletes(&before, &after);
        let mut reconciliation = reconcile(&manifest, &after_scan, LAYOUT_VERSION);
        reconciliation.deletes[0].relative_path = "wrong.md".to_string();

        let (_, divergences) = compare_deletes(&expected, &reconciliation);
        assert!(divergences
            .iter()
            .any(|item| item.kind == DivergenceKind::RetainPath));
    }

    #[test]
    fn cross_file_duplicate_ids_are_conflicts() {
        let content = "# A\n\nbody\n";
        let scan = vec![
            scanned(
                "a.md",
                content,
                vec![retain("a.md", "engram-docs--same", "", content, None)],
            ),
            scanned(
                "b.md",
                content,
                vec![retain("b.md", "engram-docs--same", "", content, None)],
            ),
        ];
        let dump = oracle(
            vec![
                oracle_file("a.md", content, None),
                oracle_file("b.md", content, None),
            ],
            vec![
                oracle_retain(1, "a.md", "engram-docs--same", "", content, None),
                oracle_retain(2, "b.md", "engram-docs--same", "", content, None),
            ],
        );
        let reconciliation = reconcile(&Manifest::default(), &scan, LAYOUT_VERSION);
        assert_eq!(reconciliation.conflicts.len(), 1);

        let ledger = compare_ledger(&dump, &reconciliation);
        assert_eq!(ledger.unexpected, 1);
        assert!(ledger
            .divergences
            .iter()
            .any(|item| item.kind == DivergenceKind::ExtraRetain));

        let conflicts = conflict_divergences(&reconciliation);
        assert_eq!(conflicts.len(), 1);
        assert_eq!(conflicts[0].kind, DivergenceKind::Conflict);
        let mut report = ShadowReport::default();
        merge_conflicts(&mut report, &reconciliation);
        assert_eq!(report.unexpected, 1);
    }

    #[test]
    fn bare_duplicate_ids_do_not_collapse() {
        let one = "first bare body\n";
        let two = "second bare body\n";
        let scan = vec![scanned(
            "a.md",
            two,
            vec![
                retain("a.md", "engram-docs--a", "", one, None),
                retain("a.md", "engram-docs--a", "", two, None),
            ],
        )];
        let dump = oracle(
            vec![oracle_file("a.md", two, None)],
            vec![
                oracle_retain(1, "a.md", "engram-docs--a", "", one, None),
                oracle_retain(2, "a.md", "engram-docs--a", "", two, None),
            ],
        );
        let reconciliation = reconcile(&Manifest::default(), &scan, LAYOUT_VERSION);
        let ledger = compare_ledger(&dump, &reconciliation);
        assert_eq!(ledger.unexpected, 1);
        assert_eq!(ledger.divergences[0].kind, DivergenceKind::ChunkKey);
    }

    #[test]
    fn whitespace_only_transition_expects_no_delete() {
        let content = "# A\n\nbody\n";
        let before = oracle(
            vec![oracle_file("a.md", content, None)],
            vec![oracle_retain(
                1,
                "a.md",
                "engram-docs--a",
                "",
                content,
                None,
            )],
        );
        let after = oracle(vec![oracle_file("a.md", " \n", None)], Vec::new());
        assert!(expected_deletes(&before, &after).is_empty());
    }

    #[test]
    fn oracle_json_round_trips_and_validates() {
        let content = "# A\n\nbody\n";
        let dump = oracle(
            vec![oracle_file("a.md", content, None)],
            vec![oracle_retain(
                1,
                "a.md",
                "engram-docs--a",
                "",
                content,
                None,
            )],
        );
        let json = serde_json::to_string(&dump).expect("serialize");
        let parsed = parse_oracle(&json).expect("parse");
        assert_eq!(parsed, dump);
        assert!(validate_oracle(&parsed).is_ok());

        let mut wrong = parsed;
        wrong.provenance.python = "3.13.5".to_string();
        assert!(validate_oracle(&wrong).is_err());
        wrong.provenance.python = "3.14.6".to_string();
        wrong.provenance.flow_source_commit.clear();
        assert!(validate_oracle(&wrong).is_err());
        wrong.provenance.flow_source_commit = "unknown".to_string();
        assert!(validate_oracle(&wrong).is_err());
    }

    #[test]
    fn hash_hex_matches_content_hash() {
        assert_eq!(
            hash_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn merge_deletes_records_parity_and_divergences() {
        let mut report = ShadowReport::default();
        merge_deletes(
            &mut report,
            DeleteParity {
                expected: 1,
                emitted: 0,
                matched: 0,
            },
            vec![Divergence::new(DivergenceKind::MissingDelete, "missing")],
        );
        assert_eq!(report.deletes.expected, 1);
        assert_eq!(report.unexpected, 1);
    }
}
