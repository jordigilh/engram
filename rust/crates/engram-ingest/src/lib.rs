//! Native Engram ingestion core (issue #214).
//!
//! This crate owns the docs half of the CocoIndex replacement: the Postgres
//! ledger, SHA-256 reconciliation, stable document/chunk IDs, heading-aware
//! Markdown chunking, deterministic synthesis metadata, and the Hindsight sink
//! that turns ledger jobs into retain/delete calls. Worker loops, per-bank
//! concurrency, metrics, and the shadow run are later #214 slices.

pub mod chunking;
pub mod document;
pub mod hashing;
pub mod ids;
pub mod jobs;
pub mod ledger;
pub mod python_text;
pub mod reconcile;
pub mod shadow;
pub mod sink;
pub mod stopwords;
pub mod store;
pub mod synthesis;

pub use chunking::{split_fixed_window, split_markdown_sections};
pub use document::{format_local_timestamp, plan_document, relative_path, RetainCall};
pub use hashing::{sha256_hex, ContentHash, HashError};
pub use ids::{chunk_document_id, document_base_id, pathlib_suffix, stable_section_key};
pub use jobs::{Job, JobError, JobState, Lease, MAX_ATTEMPTS};
pub use ledger::{
    Manifest, ManifestChunk, ManifestDocument, RecordState, SourceRecord, LAYOUT_VERSION,
};
pub use reconcile::{reconcile, DeleteReason, Reconciliation, ScannedFile};
pub use sink::{
    process_one, DeleteOutcome, HindsightSink, ProcessedJob, RetainRequest, SinkError, SinkOutcome,
};
pub use store::{
    apply_reconciliation, claim_job, complete_job, connect, fail_job, heartbeat_job, load_manifest,
    migrate, recover_expired_leases, upsert_source, ApplySummary, ClaimedJob, StoreError,
};
pub use synthesis::{
    analyze, split_sentences, synthesize_document, Analysis, Stats, Synthesis, SynthesisError,
};
