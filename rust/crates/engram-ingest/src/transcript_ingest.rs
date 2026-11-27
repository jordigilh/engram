//! Native transcript ingestion loop (issue #245).
//!
//! A transcript source is a workspace root with
//! `{prefix}*/agent-transcripts/**/*.jsonl` files. Each scan plans only the
//! signals past the durable ack watermark, writes transcript windows through
//! the shared ledger, and leaves retain/delete delivery to the bank-scoped
//! worker supervisor.
//!
//! The watermark advances in two places:
//! - a successful retain job, through `store::complete_job`, up to the first
//!   transcript job that has not succeeded;
//! - a scan with no blocking job, for a signal-free tail or a withheld
//!   (`queued`) correction.
//!
//! A failed retain therefore keeps the watermark unadvanced while the job stays
//! pending or dead in the ledger, and a replay cannot duplicate a stable window.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::PathBuf;

use chrono::{DateTime, Utc};
use postgres::Client;

use crate::document::RetainCall;
use crate::hashing::ContentHash;
use crate::ledger::{RecordState, SourceRecord, TRANSCRIPT_LAYOUT_VERSION};
use crate::reconcile::{
    DeleteChunk, DeleteReason, Reconciliation, RetainFile, TranscriptWindowMeta,
};
use crate::store::{
    apply_reconciliation, delete_transcript_watermark, delete_transcript_windows, load_manifest,
    load_pending_transcript_invalidations, load_transcript_watermark,
    load_transcript_watermark_paths, load_transcript_window_owners,
    mark_transcript_invalidation_done, refresh_transcript_watermark, upsert_source, ApplySummary,
    StoreError, TranscriptWatermarkUpdate,
};
use crate::transcript::{
    discover_transcripts, learning_windows_with_detector, parse_messages, prefix_sha256,
    window_document_id, RegexSignalDetector, SignalDetector, TranscriptError, MAX_TRANSCRIPT_BYTES,
};

pub const TRANSCRIPT_SOURCE_METADATA: &str = "cocoindex-transcript";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptSource {
    pub project: String,
    pub tag: String,
    pub root: PathBuf,
    pub bank_id: String,
    pub workspace_prefixes: Vec<String>,
}

impl TranscriptSource {
    pub fn record(&self) -> SourceRecord {
        SourceRecord::transcript(
            &self.project,
            &self.tag,
            &self.root.to_string_lossy(),
            &self.bank_id,
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ContradictionMode {
    #[default]
    Shadow,
    Live,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResolutionAction {
    Retain,
    Queued,
    AutoResolved,
}

/// Mirrors `contradiction_resolution.Resolution` from the Python flow.
#[derive(Debug, Clone, PartialEq)]
pub struct ContradictionResolution {
    pub action: ResolutionAction,
    pub superseded_memory_id: Option<String>,
    pub superseded_document_id: Option<String>,
    pub confidence: f64,
    pub explanation: String,
}

impl Default for ContradictionResolution {
    fn default() -> Self {
        Self {
            action: ResolutionAction::Retain,
            superseded_memory_id: None,
            superseded_document_id: None,
            confidence: 0.0,
            explanation: String::new(),
        }
    }
}

/// Deterministic replacement for `contradiction_resolution.resolve`. Callers
/// wire the Haiku/Sonnet classifier here; the default keeps every correction.
/// The resolver owns the Python side effects for the chosen action
/// (`pending_queue.append_pending` and `log_auto_resolved`), so the ingest layer
/// only persists the durable outcome it is handed.
pub trait ContradictionResolver {
    fn resolve(
        &self,
        bank_id: &str,
        statement: &str,
        project: Option<&str>,
    ) -> ContradictionResolution;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct RetainResolver;

impl ContradictionResolver for RetainResolver {
    fn resolve(
        &self,
        _bank_id: &str,
        _statement: &str,
        _project: Option<&str>,
    ) -> ContradictionResolution {
        ContradictionResolution::default()
    }
}

/// PATCH-invalidates a superseded memory unit. Only called in live mode.
pub trait MemoryInvalidator {
    fn invalidate(&self, bank_id: &str, memory_id: &str, reason: &str) -> bool;
}

#[derive(Debug, Clone, Copy, Default)]
pub struct NoopInvalidator;

impl MemoryInvalidator for NoopInvalidator {
    fn invalidate(&self, _bank_id: &str, _memory_id: &str, _reason: &str) -> bool {
        false
    }
}

pub struct TranscriptOptions {
    pub mode: ContradictionMode,
    pub resolver: Box<dyn ContradictionResolver>,
    pub invalidator: Box<dyn MemoryInvalidator>,
    pub detector: Box<dyn SignalDetector>,
}

impl Default for TranscriptOptions {
    fn default() -> Self {
        Self {
            mode: ContradictionMode::Shadow,
            resolver: Box::new(RetainResolver),
            invalidator: Box::new(NoopInvalidator),
            detector: Box::new(RegexSignalDetector),
        }
    }
}

impl TranscriptOptions {
    pub fn with_mode(mut self, mode: ContradictionMode) -> Self {
        self.mode = mode;
        self
    }

    pub fn with_resolver(mut self, resolver: Box<dyn ContradictionResolver>) -> Self {
        self.resolver = resolver;
        self
    }

    pub fn with_invalidator(mut self, invalidator: Box<dyn MemoryInvalidator>) -> Self {
        self.invalidator = invalidator;
        self
    }

    pub fn with_detector(mut self, detector: Box<dyn SignalDetector>) -> Self {
        self.detector = detector;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptFileOutcome {
    pub relative_path: String,
    pub transcript_id: String,
    pub message_count: i32,
    pub start_index: usize,
    pub windows_retained: usize,
    pub windows_queued: usize,
    pub windows_auto_resolved: usize,
    pub invalidated: usize,
    pub skipped: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TranscriptIngestOutcome {
    pub files: Vec<TranscriptFileOutcome>,
    pub removed: Vec<String>,
    pub summary: ApplySummary,
    pub windows_queued: usize,
    pub windows_auto_resolved: usize,
    pub invalidated: usize,
}

#[derive(Debug, thiserror::Error)]
pub enum TranscriptIngestError {
    #[error(transparent)]
    Transcript(#[from] TranscriptError),
    #[error(transparent)]
    Store(#[from] StoreError),
}

/// One transcript source pass: discover -> plan new windows -> apply ledger ->
/// refresh watermarks. `client` should already have run `store::migrate`.
struct WatermarkPlan {
    relative_path: String,
    transcript_id: String,
    message_count: i32,
    prefix_sha256: Vec<u8>,
    observed_message_count: i32,
    observed_prefix_sha256: Vec<u8>,
    reset: bool,
}

pub fn ingest_transcript_source(
    client: &mut Client,
    source: &TranscriptSource,
    options: &TranscriptOptions,
    now: DateTime<Utc>,
) -> Result<TranscriptIngestOutcome, TranscriptIngestError> {
    // Validate the root before creating a source row, matching the Python
    // warning-and-continue behavior instead of leaving an empty source behind.
    let files = discover_transcripts(&source.root, &source.workspace_prefixes)?;
    let source_id = upsert_source(client, &source.record())?;
    let manifest = load_manifest(client, source_id)?;
    let mut reconciliation = Reconciliation {
        layout_version: TRANSCRIPT_LAYOUT_VERSION,
        base_revision: manifest.revision,
        ..Reconciliation::default()
    };
    let mut outcome = TranscriptIngestOutcome::default();
    let mut seen_paths = BTreeSet::new();
    let mut window_owners: BTreeMap<String, String> = BTreeMap::new();
    let mut watermarks: Vec<WatermarkPlan> = Vec::new();

    for file in &files {
        seen_paths.insert(file.relative_path.clone());
        let relative_path = file.relative_path.clone();
        let transcript_id = file.transcript_id.clone();
        let metadata = match fs::metadata(&file.path) {
            Ok(metadata) => metadata,
            Err(error) => {
                outcome
                    .files
                    .push(skipped(&relative_path, &transcript_id, error.to_string()));
                continue;
            }
        };
        if metadata.len() > MAX_TRANSCRIPT_BYTES {
            outcome.files.push(skipped(
                &relative_path,
                &transcript_id,
                TranscriptError::TooLarge.to_string(),
            ));
            continue;
        }
        let bytes = match fs::read(&file.path) {
            Ok(bytes) => bytes,
            Err(error) => {
                outcome
                    .files
                    .push(skipped(&relative_path, &transcript_id, error.to_string()));
                continue;
            }
        };
        if bytes.len() as u64 > MAX_TRANSCRIPT_BYTES {
            outcome.files.push(skipped(
                &relative_path,
                &transcript_id,
                TranscriptError::TooLarge.to_string(),
            ));
            continue;
        }
        let messages = match parse_messages(&bytes) {
            Ok(messages) => messages,
            Err(error) => {
                outcome
                    .files
                    .push(skipped(&relative_path, &transcript_id, error.to_string()));
                continue;
            }
        };
        let message_count = messages.len() as i32;
        let stored = load_transcript_watermark(client, source_id, &relative_path)?;
        let previous = stored
            .as_ref()
            .map(|watermark| watermark.message_count)
            .unwrap_or(0)
            .max(0);
        let observed_count = stored
            .as_ref()
            .map(|watermark| watermark.observed_message_count)
            .unwrap_or(0)
            .max(0);
        let prefix_at_previous = prefix_sha256(&messages, previous as usize);
        // Compare against the last observed snapshot, not only the ack
        // watermark: a replacement can arrive while the first retain is still
        // pending and there is no acknowledged prefix yet.
        let rewritten = previous > message_count
            || match stored
                .as_ref()
                .and_then(|watermark| watermark.observed_prefix_sha256.as_deref())
            {
                Some(_) if observed_count > message_count => true,
                Some(saved) => {
                    saved != prefix_sha256(&messages, observed_count as usize).as_bytes()
                }
                None => stored
                    .as_ref()
                    .and_then(|watermark| watermark.prefix_sha256.as_deref())
                    .is_some_and(|saved| saved != prefix_at_previous.as_bytes()),
            };
        let start_index = if rewritten { 0 } else { previous as usize };
        let planned =
            learning_windows_with_detector(&messages, start_index, options.detector.as_ref());
        let existing_owners = load_transcript_window_owners(client, source_id, &transcript_id)?;
        // Same-stem transcripts in different roots are common. The window-id
        // contract keeps the stem, so a duplicate id cannot be owned by two
        // paths; skip that file instead of failing the whole source.
        if let Some((document_id, other_path)) = planned.iter().find_map(|window| {
            let document_id = window_document_id(&transcript_id, &window.text);
            existing_owners
                .get(&document_id)
                .filter(|owner| *owner != &relative_path)
                .map(|owner| (document_id.clone(), owner.clone()))
                .or_else(|| {
                    window_owners
                        .get(&document_id)
                        .filter(|owner| *owner != &relative_path)
                        .map(|owner| (document_id.clone(), owner.clone()))
                })
        }) {
            outcome.files.push(skipped(
                &relative_path,
                &transcript_id,
                format!("window id {document_id} is owned by {other_path}"),
            ));
            continue;
        }
        let mut file_outcome = TranscriptFileOutcome {
            relative_path: relative_path.clone(),
            transcript_id: transcript_id.clone(),
            message_count,
            start_index,
            windows_retained: 0,
            windows_queued: 0,
            windows_auto_resolved: 0,
            invalidated: 0,
            skipped: None,
        };
        let mut calls: Vec<RetainCall> = Vec::new();
        let mut call_positions: BTreeMap<String, usize> = BTreeMap::new();
        let current_window_ids: Option<BTreeSet<String>> = rewritten.then(|| {
            learning_windows_with_detector(&messages, 0, options.detector.as_ref())
                .into_iter()
                .map(|window| window_document_id(&transcript_id, &window.text))
                .collect()
        });
        for window in planned {
            if window.text.trim().is_empty() {
                continue;
            }
            let document_id = window_document_id(&transcript_id, &window.text);
            if existing_owners.contains_key(&document_id) {
                // Already retained or deliberately withheld. Do not re-run
                // contradiction resolution: live invalidation is a side effect
                // that must happen exactly once per window.
                continue;
            }
            window_owners.insert(document_id.clone(), relative_path.clone());
            let mut status = "retained";
            let mut tags = if source.project.is_empty() {
                Vec::new()
            } else {
                vec![source.project.clone()]
            };
            let mut invalidate_memory_id = None;
            let mut invalidate_reason = None;
            if window.text.contains("[CORRECTION]") {
                let resolution = options.resolver.resolve(
                    &source.bank_id,
                    &window.text,
                    Some(source.project.as_str()),
                );
                match resolution.action {
                    ResolutionAction::Queued => {
                        file_outcome.windows_queued += 1;
                        status = "queued";
                        reconciliation.transcript_windows.insert(
                            document_id,
                            TranscriptWindowMeta {
                                transcript_id: transcript_id.clone(),
                                relative_path: relative_path.clone(),
                                message_count,
                                status: status.to_string(),
                                invalidate_memory_id: None,
                                invalidate_reason: None,
                            },
                        );
                        continue;
                    }
                    ResolutionAction::AutoResolved => {
                        tags.extend([
                            "CORRECTION".to_string(),
                            "supersedes-prior-memory".to_string(),
                        ]);
                        file_outcome.windows_auto_resolved += 1;
                        if options.mode == ContradictionMode::Live {
                            if let Some(memory_id) = resolution.superseded_memory_id {
                                invalidate_reason = Some(format!(
                                    "superseded: {}",
                                    truncate_chars(&window.text, 200)
                                ));
                                invalidate_memory_id = Some(memory_id);
                            }
                        }
                    }
                    ResolutionAction::Retain => {}
                }
            }
            let mut metadata = BTreeMap::new();
            metadata.insert("source".to_string(), TRANSCRIPT_SOURCE_METADATA.to_string());
            metadata.insert("transcript_id".to_string(), transcript_id.clone());
            let call = RetainCall {
                document_id: document_id.clone(),
                chunk_key: document_id.clone(),
                content_sha256: ContentHash::of(window.text.as_bytes()),
                content: window.text,
                metadata,
                tags,
                timestamp: None,
            };
            reconciliation.transcript_windows.insert(
                document_id.clone(),
                TranscriptWindowMeta {
                    transcript_id: transcript_id.clone(),
                    relative_path: relative_path.clone(),
                    message_count,
                    status: status.to_string(),
                    invalidate_memory_id,
                    invalidate_reason,
                },
            );
            if let Some(position) = call_positions.get(&document_id).copied() {
                calls[position] = call;
            } else {
                call_positions.insert(document_id, calls.len());
                calls.push(call);
            }
        }
        if let Some(current_ids) = current_window_ids {
            // Rewrite/rotation: old windows that no longer exist in the new
            // content leave scope. Append-only scans never take this path, so
            // historical Python windows are preserved on normal appends.
            for chunk in manifest
                .chunks_for(&relative_path)
                .filter(|chunk| chunk.state == RecordState::Active)
            {
                if current_ids.contains(&chunk.hindsight_document_id) {
                    continue;
                }
                reconciliation.deletes.push(DeleteChunk {
                    hindsight_document_id: chunk.hindsight_document_id.clone(),
                    chunk_key: chunk.chunk_key.clone(),
                    content_sha256: chunk.content_sha256,
                    generation: chunk.generation,
                    relative_path: chunk.document_path.clone(),
                    reason: DeleteReason::ChunkRemoved,
                });
            }
            // Withheld windows have no chunk row, so their ownership rows must
            // be cleared separately or the window can never be re-planned.
            for (document_id, owner) in &existing_owners {
                if owner == &relative_path && !current_ids.contains(document_id) {
                    reconciliation
                        .transcript_window_deletes
                        .insert(document_id.clone());
                }
            }
        }
        if !calls.is_empty() {
            file_outcome.windows_retained = calls.len();
            reconciliation.retain.push(RetainFile {
                relative_path: file.relative_path.clone(),
                content_sha256: ContentHash::of(&bytes),
                size_bytes: bytes.len() as i64,
                calls,
            });
        }
        outcome.windows_queued += file_outcome.windows_queued;
        outcome.windows_auto_resolved += file_outcome.windows_auto_resolved;
        watermarks.push(WatermarkPlan {
            relative_path: relative_path.clone(),
            transcript_id,
            message_count,
            prefix_sha256: prefix_sha256(&messages, messages.len()).as_bytes().to_vec(),
            observed_message_count: message_count,
            observed_prefix_sha256: prefix_sha256(&messages, messages.len()).as_bytes().to_vec(),
            reset: rewritten,
        });
        outcome.files.push(file_outcome);
    }

    // Tombstone windows whose transcript file left the configured scope
    // (deletion, rotation, or prefix removal).
    for (path, document) in &manifest.documents {
        if document.state != RecordState::Active || seen_paths.contains(path) {
            continue;
        }
        for chunk in manifest
            .chunks_for(path)
            .filter(|chunk| chunk.state == RecordState::Active)
        {
            reconciliation.deletes.push(DeleteChunk {
                hindsight_document_id: chunk.hindsight_document_id.clone(),
                chunk_key: chunk.chunk_key.clone(),
                content_sha256: chunk.content_sha256,
                generation: chunk.generation,
                relative_path: chunk.document_path.clone(),
                reason: DeleteReason::TranscriptRemoved,
            });
        }
        reconciliation.removed_files.push(path.clone());
        outcome.removed.push(path.clone());
    }

    let summary = if reconciliation.is_empty() {
        ApplySummary::default()
    } else {
        apply_reconciliation(client, source_id, &reconciliation, now)?
    };
    outcome.summary = summary;

    // Invalidation is a durable pending intent written with the retain plan.
    // Only a successful apply reaches this point, and a crash before the PATCH
    // leaves the row pending for the next scan.
    if options.mode == ContradictionMode::Live {
        for pending in load_pending_transcript_invalidations(client, source_id)? {
            if options
                .invalidator
                .invalidate(&pending.bank_id, &pending.memory_id, &pending.reason)
            {
                mark_transcript_invalidation_done(client, source_id, &pending.document_id, now)?;
                outcome.invalidated += 1;
            }
        }
    }

    // Only after the ledger commit: a crash before this point replays the same
    // stable windows, and ownership/dedupe keys make that replay a no-op.
    for plan in &watermarks {
        refresh_transcript_watermark(
            client,
            source_id,
            TranscriptWatermarkUpdate {
                relative_path: &plan.relative_path,
                transcript_id: &plan.transcript_id,
                message_count: plan.message_count,
                prefix_sha256: &plan.prefix_sha256,
                observed_message_count: plan.observed_message_count,
                observed_prefix_sha256: &plan.observed_prefix_sha256,
                reset: plan.reset,
            },
            now,
        )?;
    }
    for path in &outcome.removed {
        delete_transcript_watermark(client, source_id, path)?;
        delete_transcript_windows(client, source_id, path)?;
    }
    // A signal-free transcript never created a document row, so its removal is
    // not visible in the manifest. Drop any watermark whose file is gone.
    let current_paths: BTreeSet<String> = files
        .iter()
        .map(|file| file.relative_path.clone())
        .collect();
    for stale in load_transcript_watermark_paths(client, source_id)? {
        if !current_paths.contains(&stale) {
            delete_transcript_watermark(client, source_id, &stale)?;
            delete_transcript_windows(client, source_id, &stale)?;
        }
    }

    Ok(outcome)
}

fn skipped(relative_path: &str, transcript_id: &str, reason: String) -> TranscriptFileOutcome {
    TranscriptFileOutcome {
        relative_path: relative_path.to_string(),
        transcript_id: transcript_id.to_string(),
        message_count: 0,
        start_index: 0,
        windows_retained: 0,
        windows_queued: 0,
        windows_auto_resolved: 0,
        invalidated: 0,
        skipped: Some(reason),
    }
}

fn truncate_chars(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}
