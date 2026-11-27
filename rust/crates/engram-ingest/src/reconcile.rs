//! SHA-256 manifest reconciliation for the docs source.
//!
//! Emitted operations follow the Python flow: a changed file re-emits a retain
//! for every chunk because synthesis metadata is document-scoped. A zero-op
//! replay proves the manifest converged. Missing chunks are deleted and
//! tombstoned, including chunks whose section disappeared from an edited file.

use std::collections::{BTreeMap, BTreeSet};

use crate::document::RetainCall;
use crate::hashing::ContentHash;
use crate::ledger::{Manifest, ManifestChunk, RecordState};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScannedFile {
    pub relative_path: String,
    pub content_sha256: ContentHash,
    pub size_bytes: i64,
    pub calls: Vec<RetainCall>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeleteReason {
    FileRemoved,
    ChunkRemoved,
    IssueRemoved,
    TranscriptRemoved,
}

impl DeleteReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::FileRemoved => "file_removed",
            Self::ChunkRemoved => "chunk_removed",
            Self::IssueRemoved => "issue_removed",
            Self::TranscriptRemoved => "transcript_removed",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeleteChunk {
    pub hindsight_document_id: String,
    pub chunk_key: String,
    pub content_sha256: ContentHash,
    pub generation: i32,
    pub relative_path: String,
    pub reason: DeleteReason,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetainFile {
    pub relative_path: String,
    pub content_sha256: ContentHash,
    pub size_bytes: i64,
    pub calls: Vec<RetainCall>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReconcileStats {
    pub scanned_files: usize,
    pub unchanged_files: usize,
    pub retained_files: usize,
    pub retained_chunks: usize,
    pub deleted_chunks: usize,
    pub tombstoned_files: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScanConflict {
    pub hindsight_document_id: String,
    pub paths: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct TranscriptWindowMeta {
    pub transcript_id: String,
    pub relative_path: String,
    pub message_count: i32,
    pub status: String,
    pub invalidate_memory_id: Option<String>,
    pub invalidate_reason: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Reconciliation {
    pub retain: Vec<RetainFile>,
    pub deletes: Vec<DeleteChunk>,
    /// Files present in the ledger that are missing from the scan.
    pub removed_files: Vec<String>,
    /// Two live paths producing the same Hindsight document id. The store
    /// rejects such a scan instead of letting processing order pick an owner.
    pub conflicts: Vec<ScanConflict>,
    pub layout_version: i32,
    /// Source revision the manifest was loaded at; the store rejects stale
    /// plans and treats a matching immediate replay as a no-op.
    pub base_revision: i64,
    pub stats: ReconcileStats,
    /// Transcript window ownership/watermark index, keyed by Hindsight
    /// document id. The store writes it in the same transaction as the chunk.
    pub transcript_windows: BTreeMap<String, TranscriptWindowMeta>,
    /// Transcript ownership rows to remove because their window vanished from a
    /// rewritten/rotated file (including withheld windows with no chunk row).
    pub transcript_window_deletes: BTreeSet<String>,
}

impl Reconciliation {
    pub fn is_empty(&self) -> bool {
        self.retain.is_empty()
            && self.deletes.is_empty()
            && self.removed_files.is_empty()
            && self.conflicts.is_empty()
            && self.transcript_windows.is_empty()
            && self.transcript_window_deletes.is_empty()
    }
}

pub fn reconcile(manifest: &Manifest, scan: &[ScannedFile], layout_version: i32) -> Reconciliation {
    reconcile_with_removed_reason(manifest, scan, layout_version, DeleteReason::FileRemoved)
}

/// Like [`reconcile`], but lets the caller name why a whole document left the
/// scan. Issues use [`DeleteReason::IssueRemoved`]; docs keep `file_removed`.
pub fn reconcile_with_removed_reason(
    manifest: &Manifest,
    scan: &[ScannedFile],
    layout_version: i32,
    removed_reason: DeleteReason,
) -> Reconciliation {
    let scanned: BTreeMap<&str, &ScannedFile> = scan
        .iter()
        .map(|file| (file.relative_path.as_str(), file))
        .collect();
    let mut result = Reconciliation {
        layout_version,
        base_revision: manifest.revision,
        stats: ReconcileStats {
            scanned_files: scanned.len(),
            ..ReconcileStats::default()
        },
        ..Reconciliation::default()
    };

    for (path, file) in &scanned {
        if manifest_matches(manifest, file, layout_version) {
            result.stats.unchanged_files += 1;
            continue;
        }
        if file.calls.is_empty() {
            // Python `process_doc_file` returns for blank content and does not
            // delete previously retained chunks. Keep that behavior.
            continue;
        }

        // Duplicate heading lines produce the same Python document id twice.
        // Hindsight's final state is the last retain, so the ledger keeps one
        // chunk per id (the last occurrence) to stay replay-stable.
        let calls = unique_calls(&file.calls);
        let new_ids: BTreeSet<&str> = calls.iter().map(|call| call.document_id.as_str()).collect();
        for chunk in manifest
            .chunks_for(path)
            .filter(|chunk| chunk.state == RecordState::Active)
        {
            if !new_ids.contains(chunk.hindsight_document_id.as_str()) {
                push_delete(&mut result, chunk, DeleteReason::ChunkRemoved);
            }
        }

        result.stats.retained_files += 1;
        result.stats.retained_chunks += calls.len();
        result.retain.push(RetainFile {
            relative_path: file.relative_path.clone(),
            content_sha256: file.content_sha256,
            size_bytes: file.size_bytes,
            calls,
        });
    }

    for (path, document) in &manifest.documents {
        if scanned.contains_key(path.as_str()) || document.state != RecordState::Active {
            continue;
        }
        for chunk in manifest
            .chunks_for(path)
            .filter(|chunk| chunk.state == RecordState::Active)
        {
            push_delete(&mut result, chunk, removed_reason);
        }
        result.removed_files.push(path.clone());
        result.stats.tombstoned_files += 1;
    }

    // A rename can keep the same base id (e.g. `a/b.md` -> `a--b.md`). The
    // chunk is retained under the new path, so the old path's delete is an
    // ownership transfer, not a sink delete.
    let retained_ids: BTreeSet<&str> = result
        .retain
        .iter()
        .flat_map(|file| file.calls.iter().map(|call| call.document_id.as_str()))
        .collect();
    result
        .deletes
        .retain(|delete| !retained_ids.contains(delete.hindsight_document_id.as_str()));
    result.stats.deleted_chunks = result.deletes.len();

    // Two live paths with one id are ambiguous; refuse rather than assigning
    // the chunk to whichever path happens to be processed last.
    let mut live_paths: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for file in scan {
        for call in unique_calls(&file.calls) {
            live_paths
                .entry(call.document_id)
                .or_default()
                .insert(file.relative_path.clone());
        }
    }
    for (document_id, paths) in live_paths {
        if paths.len() > 1 {
            result.conflicts.push(ScanConflict {
                hindsight_document_id: document_id,
                paths: paths.into_iter().collect(),
            });
        }
    }

    result
}

fn manifest_matches(manifest: &Manifest, file: &ScannedFile, layout_version: i32) -> bool {
    let Some(document) = manifest.documents.get(&file.relative_path) else {
        return false;
    };
    if document.state != RecordState::Active
        || document.layout_version != layout_version
        || document.content_sha256 != file.content_sha256
        || file.calls.is_empty()
    {
        return false;
    }
    let calls = unique_calls(&file.calls);
    let active: Vec<&ManifestChunk> = manifest
        .chunks_for(&file.relative_path)
        .filter(|chunk| chunk.state == RecordState::Active)
        .collect();
    if active.len() != calls.len() {
        return false;
    }
    calls.iter().all(|call| {
        manifest.chunks.get(&call.document_id).is_some_and(|chunk| {
            chunk.state == RecordState::Active && chunk.content_sha256 == call.content_sha256
        })
    })
}

fn unique_calls(calls: &[RetainCall]) -> Vec<RetainCall> {
    let mut last_position: BTreeMap<&str, usize> = BTreeMap::new();
    for (index, call) in calls.iter().enumerate() {
        last_position.insert(call.document_id.as_str(), index);
    }
    let mut ordered: Vec<(usize, RetainCall)> = last_position
        .values()
        .map(|&index| (index, calls[index].clone()))
        .collect();
    ordered.sort_by_key(|(index, _)| *index);
    ordered.into_iter().map(|(_, call)| call).collect()
}

fn push_delete(result: &mut Reconciliation, chunk: &ManifestChunk, reason: DeleteReason) {
    result.stats.deleted_chunks += 1;
    result.deletes.push(DeleteChunk {
        hindsight_document_id: chunk.hindsight_document_id.clone(),
        chunk_key: chunk.chunk_key.clone(),
        content_sha256: chunk.content_sha256,
        generation: chunk.generation,
        relative_path: chunk.document_path.clone(),
        reason,
    });
}

/// Build a manifest-shaped snapshot from a scan, used by tests and the local
/// convergence harness to model the state after a successful apply.
pub fn manifest_from_scan(scan: &[ScannedFile], layout_version: i32) -> Manifest {
    let mut manifest = Manifest::default();
    for file in scan {
        if file.calls.is_empty() {
            continue;
        }
        manifest.documents.insert(
            file.relative_path.clone(),
            crate::ledger::ManifestDocument {
                relative_path: file.relative_path.clone(),
                content_sha256: file.content_sha256,
                size_bytes: file.size_bytes,
                layout_version,
                state: RecordState::Active,
            },
        );
        for (ordinal, call) in unique_calls(&file.calls).iter().enumerate() {
            manifest.chunks.insert(
                call.document_id.clone(),
                ManifestChunk {
                    document_path: file.relative_path.clone(),
                    hindsight_document_id: call.document_id.clone(),
                    chunk_key: call.chunk_key.clone(),
                    content_sha256: call.content_sha256,
                    ordinal: ordinal as i32,
                    generation: 1,
                    state: RecordState::Active,
                },
            );
        }
    }
    manifest
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::document::plan_document;
    use crate::ledger::LAYOUT_VERSION;

    fn scan_file(path: &str, content: &str) -> ScannedFile {
        let calls = plan_document("repo", path, content, None).unwrap();
        ScannedFile {
            relative_path: path.to_string(),
            content_sha256: ContentHash::of(content.as_bytes()),
            size_bytes: content.len() as i64,
            calls,
        }
    }

    #[test]
    fn replay_is_idempotent() {
        let scan = vec![scan_file(
            "a.md",
            "# A\n\nSome long body text for section A.",
        )];
        let manifest = manifest_from_scan(&scan, LAYOUT_VERSION);
        let result = reconcile(&manifest, &scan, LAYOUT_VERSION);
        assert!(result.is_empty());
        assert_eq!(result.stats.unchanged_files, 1);
    }

    #[test]
    fn changed_file_retains_every_chunk() {
        let old = vec![scan_file(
            "a.md",
            "# A\n\nAlpha body long enough.\n\n## B\n\nBeta body long enough.",
        )];
        let manifest = manifest_from_scan(&old, LAYOUT_VERSION);
        let new = vec![scan_file(
            "a.md",
            "# A\n\nAlpha body changed but still long enough.\n\n## B\n\nBeta body long enough.",
        )];
        let result = reconcile(&manifest, &new, LAYOUT_VERSION);
        assert_eq!(result.retain.len(), 1);
        assert_eq!(result.retain[0].calls.len(), 2);
        assert!(result.deletes.is_empty());
    }

    #[test]
    fn removed_heading_deletes_and_tombstones_its_chunk() {
        let old = vec![scan_file(
            "a.md",
            "# A\n\nAlpha body long enough.\n\n## B\n\nBeta body long enough.",
        )];
        let manifest = manifest_from_scan(&old, LAYOUT_VERSION);
        let new = vec![scan_file("a.md", "# A\n\nAlpha body long enough.")];
        let result = reconcile(&manifest, &new, LAYOUT_VERSION);
        assert_eq!(result.deletes.len(), 1);
        assert_eq!(result.deletes[0].reason, DeleteReason::ChunkRemoved);
        assert_eq!(result.retain[0].calls.len(), 1);
    }

    #[test]
    fn removed_file_deletes_every_chunk() {
        let scan = vec![scan_file(
            "a.md",
            "# A\n\nAlpha body long enough.\n\n## B\n\nBeta body long enough.",
        )];
        let manifest = manifest_from_scan(&scan, LAYOUT_VERSION);
        let result = reconcile(&manifest, &[], LAYOUT_VERSION);
        assert_eq!(result.removed_files, vec!["a.md"]);
        assert_eq!(result.deletes.len(), 2);
        assert!(result
            .deletes
            .iter()
            .all(|delete| delete.reason == DeleteReason::FileRemoved));
    }

    #[test]
    fn layout_version_change_forces_every_chunk() {
        let scan = vec![scan_file("a.md", "# A\n\nAlpha body long enough.")];
        let manifest = manifest_from_scan(&scan, LAYOUT_VERSION);
        let result = reconcile(&manifest, &scan, LAYOUT_VERSION + 1);
        assert_eq!(result.retain.len(), 1);
        assert_eq!(result.stats.unchanged_files, 0);
    }

    #[test]
    fn whitespace_content_emits_nothing() {
        let old = vec![scan_file("a.md", "# A\n\nAlpha body long enough.")];
        let manifest = manifest_from_scan(&old, LAYOUT_VERSION);
        let new = vec![scan_file("a.md", "  \n\t")];
        let result = reconcile(&manifest, &new, LAYOUT_VERSION);
        assert!(result.is_empty());
    }

    #[test]
    fn new_first_heading_shifts_the_bare_id() {
        let old = vec![scan_file(
            "a.md",
            "Preamble text that is long enough.\n\n# A\n\nAlpha body long enough.",
        )];
        let manifest = manifest_from_scan(&old, LAYOUT_VERSION);
        let new = vec![scan_file(
            "a.md",
            "# New\n\nPreamble text that is long enough.\n\n# A\n\nAlpha body long enough.",
        )];
        let result = reconcile(&manifest, &new, LAYOUT_VERSION);
        assert_eq!(result.retain[0].calls[0].document_id, "repo--a");
        assert!(result.retain[0].calls[0].content.starts_with("# New"));
        assert!(old[0].calls[0].content.starts_with("Preamble"));
        assert_eq!(
            result.retain[0].calls[1].document_id,
            old[0].calls[1].document_id
        );
    }

    #[test]
    fn missing_chunk_repairs_a_partial_apply_and_then_converges() {
        let scan = vec![scan_file(
            "a.md",
            "# A\n\nAlpha body long enough.\n\n## B\n\nBeta body long enough.",
        )];
        let mut manifest = manifest_from_scan(&scan, LAYOUT_VERSION);
        let last = manifest.chunks.keys().last().cloned().unwrap();
        manifest.chunks.get_mut(&last).unwrap().state = RecordState::Tombstoned;
        let repair = reconcile(&manifest, &scan, LAYOUT_VERSION);
        assert_eq!(repair.retain.len(), 1);
        assert_eq!(repair.retain[0].calls.len(), 2);

        let repaired = manifest_from_scan(&scan, LAYOUT_VERSION);
        assert!(reconcile(&repaired, &scan, LAYOUT_VERSION).is_empty());
    }

    #[test]
    fn rename_with_same_base_id_transfers_ownership() {
        let old = vec![scan_file("a/b.md", "# A\n\nAlpha body long enough.")];
        let manifest = manifest_from_scan(&old, LAYOUT_VERSION);
        let new = vec![scan_file("a--b.md", "# A\n\nAlpha body long enough.")];
        let result = reconcile(&manifest, &new, LAYOUT_VERSION);
        assert_eq!(result.retain.len(), 1);
        assert_eq!(result.removed_files, vec!["a/b.md"]);
        assert!(result.deletes.is_empty(), "{:?}", result.deletes);
    }

    #[test]
    fn two_live_paths_with_one_id_are_a_conflict() {
        let scan = vec![
            scan_file("a/b.md", "# A\n\nAlpha body long enough."),
            scan_file("a--b.md", "# A\n\nAlpha body long enough."),
        ];
        let result = reconcile(&Manifest::default(), &scan, LAYOUT_VERSION);
        assert_eq!(result.conflicts.len(), 1);
    }

    #[test]
    fn duplicate_heading_lines_converge_on_one_chunk() {
        let content = "Preamble long enough for a section.\n\n## Notes\n\nFirst notes body.\n\n## Notes\n\nSecond notes body.";
        let scan = vec![scan_file("a.md", content)];
        assert_eq!(scan[0].calls.len(), 3);
        let manifest = manifest_from_scan(&scan, LAYOUT_VERSION);
        let replay = reconcile(&manifest, &scan, LAYOUT_VERSION);
        assert!(replay.is_empty(), "{replay:?}");

        let first = reconcile(&Manifest::default(), &scan, LAYOUT_VERSION);
        assert_eq!(first.retain[0].calls.len(), 2);
        assert!(first.retain[0].calls[1]
            .content
            .contains("Second notes body"));
    }
}
