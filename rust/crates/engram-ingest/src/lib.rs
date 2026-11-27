//! Native Engram ingestion core (issue #214).
//!
//! This crate owns the docs and issues halves of the CocoIndex replacement:
//! the Postgres ledger, SHA-256 reconciliation, stable document/chunk IDs,
//! heading-aware Markdown chunking, deterministic synthesis metadata, the
//! Hindsight sink, the bank-scoped worker loop, GitHub/Jira issue
//! serialization/polling, and the docs runtime entrypoint that performs
//! startup/periodic reconciliation and supervises workers. Each issue stream
//! polls a full snapshot, reconciles it into the same ledger, and reuses the
//! worker supervisor for retain and delete jobs.

pub mod boards;
pub mod chunking;
pub mod config;
pub mod discussions;
pub mod document;
pub mod hashing;
pub mod http;
pub mod ids;
pub mod issue_poll;
pub mod issues;
pub mod jobs;
pub mod ledger;
pub mod python_text;
pub mod reconcile;
pub mod runtime;
pub mod scan;
pub mod signals;
pub mod sink;
pub mod stopwords;
pub mod store;
pub mod synthesis;
pub mod worker;

pub use boards::{
    board_content, board_slug, plan_board, python_now_local_timestamp, Board, BoardItem,
    BOARD_STATUS_ORDER,
};
pub use chunking::{split_fixed_window, split_markdown_sections};
pub use config::{DocsSource, RuntimeConfig, DEFAULT_SCAN_INTERVAL_SECS};
pub use discussions::{discussion_header, plan_discussion, Discussion, DiscussionComment};
pub use document::{format_local_timestamp, plan_document, relative_path, RetainCall};
pub use hashing::{sha256_hex, ContentHash, HashError};
pub use http::{HttpResponse, HttpTransport, ReqwestTransport};
pub use ids::{chunk_document_id, document_base_id, pathlib_suffix, stable_section_key};
pub use issue_poll::{
    apply_snapshot, ingest_issue_stream, poll_board, poll_discussions, poll_github,
    poll_issue_stream, poll_jira, stream_fingerprint, validate_bank_streams, IssueIngestError,
    IssueIngestOutcome, IssuePollOutcome, IssueProvider, IssueSnapshot, IssueStream, PollError,
    DEFAULT_GITHUB_API,
};
pub use issues::{
    adf_to_text, issue_document_id, normalize_github_login, plan_github_issue, plan_jira_issue,
    short_repo, split_issue_sections, GitHubComment, GitHubIssue, GitHubKind, IssueError,
    IssuePlan, JiraComment, JiraIssue, ISSUE_CHUNK_OVERLAP, ISSUE_CHUNK_SIZE,
};
pub use jobs::{Job, JobError, JobState, Lease, MAX_ATTEMPTS};
pub use ledger::{
    is_valid_bank_id, Manifest, ManifestChunk, ManifestDocument, RecordState, SourceRecord,
    ISSUE_LAYOUT_VERSION, LAYOUT_VERSION,
};
pub use reconcile::{
    reconcile, reconcile_with_removed_reason, DeleteReason, Reconciliation, ScannedFile,
};
pub use runtime::{PrepareReport, Runtime, RuntimeError, RuntimeHandle, SourceOutcome};
pub use scan::{scan_source, ScanError, ScanReport};
pub use sink::{
    process_one, process_one_reporting, DeleteOutcome, HindsightSink, ProcessStep, ProcessedJob,
    RetainRequest, SinkError, SinkOutcome,
};
pub use store::{
    apply_reconciliation, claim_job, complete_job, connect, count_drain_jobs,
    count_stranded_expired_leases, fail_job, heartbeat_job, lease_is_active, load_issue_cursor,
    load_manifest, load_source_revision, migrate, recover_expired_leases,
    recover_expired_leases_for_banks, recover_expired_leases_in_bank,
    recover_expired_leases_outside_banks, save_issue_cursor, upsert_source, ApplySummary,
    ClaimedJob, DrainCounts, FailDisposition, IssueCursor, RecoveryReport, StoreError,
    RECOVERY_BATCH,
};
pub use synthesis::{
    analyze, split_sentences, synthesize_document, Analysis, Stats, Synthesis, SynthesisError,
};
pub use worker::{
    BackoffPolicy, BankConfig, Clock, JobDriver, Shutdown, SlotHealth, SlotState, WorkerConfig,
    WorkerError, WorkerHandle, WorkerMetrics, WorkerMetricsSnapshot, WorkerSupervisor,
};
