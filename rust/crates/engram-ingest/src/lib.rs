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
pub mod sink;
pub mod stopwords;
pub mod store;
pub mod synthesis;
pub mod worker;

pub use chunking::{split_fixed_window, split_markdown_sections};
pub use document::{format_local_timestamp, plan_document, relative_path, RetainCall};
pub use hashing::{sha256_hex, ContentHash, HashError};
pub use ids::{chunk_document_id, document_base_id, pathlib_suffix, stable_section_key};
pub use jobs::{Job, JobError, JobState, Lease, MAX_ATTEMPTS};
pub use ledger::{
    is_valid_bank_id, Manifest, ManifestChunk, ManifestDocument, RecordState, SourceRecord,
    LAYOUT_VERSION,
};
pub use reconcile::{reconcile, DeleteReason, Reconciliation, ScannedFile};
pub use sink::{
    process_one, process_one_reporting, DeleteOutcome, HindsightSink, ProcessStep, ProcessedJob,
    RetainRequest, SinkError, SinkOutcome,
};
pub use store::{
    apply_reconciliation, claim_job, complete_job, connect, count_stranded_expired_leases,
    fail_job, heartbeat_job, lease_is_active, load_manifest, migrate, recover_expired_leases,
    recover_expired_leases_for_banks, recover_expired_leases_in_bank,
    recover_expired_leases_outside_banks, upsert_source, ApplySummary, ClaimedJob, FailDisposition,
    RecoveryReport, StoreError, RECOVERY_BATCH,
};
pub use synthesis::{
    analyze, split_sentences, synthesize_document, Analysis, Stats, Synthesis, SynthesisError,
};
pub use worker::{
    BackoffPolicy, BankConfig, Clock, JobDriver, Shutdown, SlotHealth, SlotState, WorkerConfig,
    WorkerError, WorkerHandle, WorkerMetrics, WorkerMetricsSnapshot, WorkerSupervisor,
};
