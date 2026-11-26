//! Runtime orchestration for the native docs ingestion path (issue #242).
//!
//! `Runtime::start` performs the one-time ledger guard and startup
//! reconciliation, then starts the bank-scoped `WorkerSupervisor` with one
//! shared Hindsight sink. `RuntimeHandle::run` owns the bounded periodic scan
//! and converts SIGINT/SIGTERM cancellation into a worker drain and join.

use std::time::{Duration, Instant};

use chrono::Utc;
use postgres::Client;

use crate::config::{ConfigError, DocsSource, RuntimeConfig};
use crate::reconcile::{reconcile, Reconciliation};
use crate::scan::{scan_source_with_shutdown, ScanError};
use crate::sink::{HindsightSink, SinkError};
use crate::store::{
    apply_reconciliation, connect, count_drain_jobs, load_manifest, migrate, upsert_source,
    ApplySummary, DrainCounts, RecoveryReport, StoreError,
};
use crate::worker::{
    BankConfig, Shutdown, SlotHealth, WorkerConfig, WorkerError, WorkerHandle,
    WorkerMetricsSnapshot, WorkerSupervisor,
};

pub const DEFAULT_DRAIN_TIMEOUT: Duration = Duration::from_secs(120);
pub const DRAIN_POLL_INTERVAL: Duration = Duration::from_millis(100);

const SCAN_STATEMENT_TIMEOUT: Duration = Duration::from_secs(30);
const SCAN_LOCK_TIMEOUT: Duration = Duration::from_secs(5);
const WORKER_JOIN_TIMEOUT: Duration = Duration::from_secs(90);

#[derive(Debug, thiserror::Error)]
pub enum RuntimeError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Sink(#[from] SinkError),
    #[error(transparent)]
    Worker(#[from] WorkerError),
    #[error(transparent)]
    Scan(#[from] ScanError),
    #[error("drain timed out with {counts:?} still running")]
    DrainTimeout { counts: DrainCounts },
    #[error("drain finished with failed jobs: {counts:?}")]
    DrainFailed { counts: DrainCounts },
    #[error("runtime cancelled during startup")]
    Cancelled,
}

/// What one configured source contributed to a prepare or periodic scan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceOutcome {
    Applied {
        project: String,
        tag: String,
        root: String,
        scanned_files: usize,
        summary: ApplySummary,
    },
    Skipped {
        project: String,
        tag: String,
        root: String,
        reason: String,
    },
    Deferred {
        project: String,
        tag: String,
        root: String,
        reason: String,
    },
    Guarded {
        project: String,
        tag: String,
        root: String,
        reason: String,
    },
}

impl SourceOutcome {
    pub fn label(&self) -> String {
        match self {
            Self::Applied {
                project,
                tag,
                scanned_files,
                summary,
                ..
            } => format!(
                "{project}/{tag}: {scanned_files} scanned, {} files retained ({} chunks), {} deleted, {} tombstoned",
                summary.retained_files,
                summary.retained_chunks,
                summary.deleted_chunks,
                summary.tombstoned_files
            ),
            Self::Skipped {
                project,
                tag,
                reason,
                ..
            } => format!("{project}/{tag}: skipped: {reason}"),
            Self::Deferred {
                project,
                tag,
                reason,
                ..
            } => format!("{project}/{tag}: deferred: {reason}"),
            Self::Guarded {
                project,
                tag,
                reason,
                ..
            } => format!("{project}/{tag}: guarded: {reason}"),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PrepareReport {
    pub outcomes: Vec<SourceOutcome>,
}

pub struct Runtime {
    config: RuntimeConfig,
    sink: HindsightSink,
}

impl Runtime {
    pub fn new(config: RuntimeConfig) -> Result<Self, RuntimeError> {
        let sink = HindsightSink::new(&config.hindsight_url)?;
        Ok(Self { config, sink })
    }

    pub fn config(&self) -> &RuntimeConfig {
        &self.config
    }

    /// Fail-loud ledger guard plus the startup scan/apply pass.
    pub fn prepare(&self) -> Result<PrepareReport, RuntimeError> {
        self.prepare_with_shutdown(None)
    }

    fn prepare_with_shutdown(
        &self,
        shutdown: Option<&Shutdown>,
    ) -> Result<PrepareReport, RuntimeError> {
        // Migrations get their own connection with the ledger guard's own
        // generous lock budget; the scan connection then gets tight timeouts.
        let mut migration = connect(&self.config.database_url)?;
        migrate(&mut migration)?;
        drop(migration);
        let mut client = self.connect_for_scan()?;
        let outcomes = self.scan_sources(&mut client, shutdown)?;
        Ok(PrepareReport { outcomes })
    }

    /// One reconciliation pass without a ledger migration. Used by the
    /// periodic loop after `prepare` has already proven the schema.
    pub fn scan_once(&self) -> Result<Vec<SourceOutcome>, RuntimeError> {
        self.scan_once_with_shutdown(None)
    }

    fn scan_once_with_shutdown(
        &self,
        shutdown: Option<&Shutdown>,
    ) -> Result<Vec<SourceOutcome>, RuntimeError> {
        let mut client = self.connect_for_scan()?;
        self.scan_sources(&mut client, shutdown)
    }

    /// Prepare and start serving the configured banks.
    pub fn start(self, shutdown: Shutdown) -> Result<RuntimeHandle, RuntimeError> {
        let run_started = Utc::now();
        let prepared = self.prepare_with_shutdown(Some(&shutdown))?;
        if shutdown.is_cancelled() {
            return Err(RuntimeError::Cancelled);
        }
        let bank_ids = self.config.bank_ids();
        let banks: Vec<BankConfig> = bank_ids
            .iter()
            .map(|bank_id| BankConfig {
                bank_id: bank_id.clone(),
                concurrency: self.config.concurrency,
            })
            .collect();
        let mut worker_config =
            WorkerConfig::new(self.config.database_url.clone(), self.sink.clone(), banks);
        worker_config.owner_prefix = "engram-ingest-runtime".to_string();
        let worker = WorkerSupervisor::start(worker_config)?;
        Ok(RuntimeHandle {
            runtime: self,
            shutdown,
            worker,
            prepared,
            bank_ids,
            run_started,
        })
    }

    fn connect_for_scan(&self) -> Result<Client, RuntimeError> {
        let mut client = connect_with_timeout(&self.config.database_url)?;
        client
            .batch_execute(&format!(
                "SET statement_timeout = '{}ms'; SET lock_timeout = '{}ms'",
                SCAN_STATEMENT_TIMEOUT.as_millis(),
                SCAN_LOCK_TIMEOUT.as_millis()
            ))
            .map_err(StoreError::Postgres)?;
        Ok(client)
    }

    fn scan_sources(
        &self,
        client: &mut Client,
        shutdown: Option<&Shutdown>,
    ) -> Result<Vec<SourceOutcome>, RuntimeError> {
        let mut outcomes = Vec::with_capacity(self.config.sources.len());
        for source in &self.config.sources {
            if shutdown.is_some_and(Shutdown::is_cancelled) {
                break;
            }
            match self.scan_source(client, source, shutdown)? {
                Some(outcome) => outcomes.push(outcome),
                None => break,
            }
        }
        Ok(outcomes)
    }

    fn scan_source(
        &self,
        client: &mut Client,
        source: &DocsSource,
        shutdown: Option<&Shutdown>,
    ) -> Result<Option<SourceOutcome>, RuntimeError> {
        let root = source.root.to_string_lossy().to_string();
        let (files, report) = match scan_source_with_shutdown(source, shutdown) {
            Ok(result) => result,
            Err(ScanError::Cancelled) => return Ok(None),
            Err(ScanError::MissingRoot(_)) => {
                return Ok(Some(SourceOutcome::Skipped {
                    project: source.project.clone(),
                    tag: source.tag.clone(),
                    root,
                    reason: "root does not exist; leaving the existing manifest untouched"
                        .to_string(),
                }))
            }
            Err(
                error @ (ScanError::Pattern { .. }
                | ScanError::Plan { .. }
                | ScanError::PathCollision(_)),
            ) => return Err(RuntimeError::Scan(error)),
            Err(error) => {
                return Ok(Some(SourceOutcome::Skipped {
                    project: source.project.clone(),
                    tag: source.tag.clone(),
                    root,
                    reason: format!(
                        "scan aborted to protect the manifest from a partial snapshot: {error}"
                    ),
                }))
            }
        };

        let record = crate::ledger::SourceRecord {
            project: source.project.clone(),
            tag: source.tag.clone(),
            root: root.clone(),
            bank_id: source.bank_id.clone(),
            docs_include: source.docs_include.clone(),
            docs_exclude: source.docs_exclude.clone(),
            document_format: crate::ledger::DEFAULT_DOCUMENT_FORMAT.to_string(),
            source_kind: crate::ledger::DEFAULT_SOURCE_KIND.to_string(),
        };
        let source_id = upsert_source(client, &record)?;
        let manifest = load_manifest(client, source_id)?;
        let active_documents = manifest
            .documents
            .values()
            .filter(|document| document.state == crate::ledger::RecordState::Active)
            .count();
        if files.is_empty() && active_documents > 0 && !self.config.allow_empty_root {
            return Ok(Some(SourceOutcome::Guarded {
                project: source.project.clone(),
                tag: source.tag.clone(),
                root,
                reason: format!(
                    "root produced an empty snapshot while {active_documents} document(s) are \
                     active; pass --allow-empty-root to treat this as a real deletion"
                ),
            }));
        }
        let plan: Reconciliation = reconcile(&manifest, &files, crate::ledger::LAYOUT_VERSION);
        match apply_reconciliation(client, source_id, &plan, Utc::now()) {
            Ok(summary) => Ok(Some(SourceOutcome::Applied {
                project: source.project.clone(),
                tag: source.tag.clone(),
                root,
                scanned_files: report.matched_files,
                summary,
            })),
            Err(StoreError::StaleReconciliation) => Ok(Some(SourceOutcome::Deferred {
                project: source.project.clone(),
                tag: source.tag.clone(),
                root,
                reason: "another apply advanced the source revision; retrying next scan"
                    .to_string(),
            })),
            Err(error) => Err(error.into()),
        }
    }
}

fn connect_with_timeout(database_url: &str) -> Result<Client, StoreError> {
    let mut client = connect(database_url)?;
    client.batch_execute(&format!(
        "SET statement_timeout = '{}ms'; SET lock_timeout = '{}ms'",
        SCAN_STATEMENT_TIMEOUT.as_millis(),
        SCAN_LOCK_TIMEOUT.as_millis()
    ))?;
    Ok(client)
}

/// Running runtime. Exposes the worker's shutdown, health, metrics, and
/// startup-recovery views, and owns the periodic scan loop.
pub struct RuntimeHandle {
    runtime: Runtime,
    shutdown: Shutdown,
    worker: WorkerHandle,
    prepared: PrepareReport,
    bank_ids: Vec<String>,
    run_started: chrono::DateTime<Utc>,
}

impl RuntimeHandle {
    pub fn prepared(&self) -> &PrepareReport {
        &self.prepared
    }

    pub fn bank_ids(&self) -> &[String] {
        &self.bank_ids
    }

    pub fn shutdown(&self) {
        self.shutdown.cancel();
    }

    pub fn health(&self) -> Vec<SlotHealth> {
        self.worker.health()
    }

    pub fn snapshot(&self) -> Vec<WorkerMetricsSnapshot> {
        self.worker.snapshot()
    }

    pub fn metrics(&self) -> String {
        self.worker.prometheus()
    }

    pub fn startup_recovery(&self) -> RecoveryReport {
        self.worker.startup_recovery()
    }

    /// Resident mode: scan on every interval until shutdown, then drain.
    pub fn run(self) -> Result<(), RuntimeError> {
        let mut failure = None;
        loop {
            if !self.shutdown.sleep(self.runtime.config.scan_interval) {
                break;
            }
            match self.runtime.scan_once_with_shutdown(Some(&self.shutdown)) {
                Ok(outcomes) => log_outcomes(&outcomes),
                Err(RuntimeError::Store(
                    error @ (StoreError::BankChanged(_) | StoreError::ScanConflict(_)),
                )) => {
                    failure = Some(RuntimeError::Store(error));
                    break;
                }
                Err(error) => eprintln!("engram-ingest: periodic scan failed: {error}"),
            }
        }
        self.finish()?;
        match failure {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }

    /// One-shot mode: wait until no ready-pending job or live lease remains.
    pub fn drain(self, timeout: Duration) -> Result<(), RuntimeError> {
        let deadline = Instant::now() + timeout;
        let mut client = match self.runtime.connect_for_scan() {
            Ok(client) => client,
            Err(error) => {
                let _ = self.finish_inner(None);
                return Err(error);
            }
        };
        loop {
            if self.shutdown.is_cancelled() {
                break;
            }
            let counts = match count_drain_jobs(&mut client, &self.bank_ids, self.run_started) {
                Ok(counts) => counts,
                Err(error) => {
                    let _ = self.finish_inner(None);
                    return Err(error.into());
                }
            };
            if counts.dead > 0 {
                let _ = self.finish_inner(None);
                return Err(RuntimeError::DrainFailed { counts });
            }
            if counts.is_empty() {
                break;
            }
            if Instant::now() >= deadline {
                let _ = self.finish_inner(None);
                return Err(RuntimeError::DrainTimeout { counts });
            }
            if !self.shutdown.sleep(DRAIN_POLL_INTERVAL) {
                break;
            }
        }
        self.finish()
    }

    fn finish(self) -> Result<(), RuntimeError> {
        self.finish_inner(None)
    }

    fn finish_inner(self, failure: Option<RuntimeError>) -> Result<(), RuntimeError> {
        let metrics = self.worker.prometheus();
        self.worker.shutdown();
        let joined = self.worker.join_timeout(WORKER_JOIN_TIMEOUT);
        if !metrics.is_empty() {
            eprintln!("{metrics}");
        }
        match (failure, joined) {
            (Some(error), _) => Err(error),
            (None, result) => result.map_err(RuntimeError::Worker),
        }
    }
}

fn log_outcomes(outcomes: &[SourceOutcome]) {
    for outcome in outcomes {
        eprintln!("engram-ingest: {}", outcome.label());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn outcome_labels_are_human_readable() {
        let applied = SourceOutcome::Applied {
            project: "example".to_string(),
            tag: "repo".to_string(),
            root: "/tmp/repo".to_string(),
            scanned_files: 3,
            summary: ApplySummary {
                retained_files: 2,
                retained_chunks: 5,
                deleted_chunks: 1,
                tombstoned_files: 1,
                jobs_created: 7,
            },
        };
        assert!(applied.label().contains("example/repo"));
        assert!(applied.label().contains("2 files retained"));
        let skipped = SourceOutcome::Skipped {
            project: "example".to_string(),
            tag: "repo".to_string(),
            root: "/tmp/repo".to_string(),
            reason: "missing".to_string(),
        };
        assert!(skipped.label().contains("skipped: missing"));
        let deferred = SourceOutcome::Deferred {
            project: "example".to_string(),
            tag: "repo".to_string(),
            root: "/tmp/repo".to_string(),
            reason: "stale".to_string(),
        };
        assert!(deferred.label().contains("deferred: stale"));
        let guarded = SourceOutcome::Guarded {
            project: "example".to_string(),
            tag: "repo".to_string(),
            root: "/tmp/repo".to_string(),
            reason: "empty".to_string(),
        };
        assert!(guarded.label().contains("guarded: empty"));
    }

    #[test]
    fn drain_counts_empty_semantics() {
        assert!(DrainCounts::default().is_empty());
        assert!(!DrainCounts {
            pending: 1,
            leased: 0,
            dead: 0,
        }
        .is_empty());
        assert!(!DrainCounts {
            pending: 0,
            leased: 1,
            dead: 0,
        }
        .is_empty());
        assert!(!DrainCounts {
            pending: 0,
            leased: 0,
            dead: 1,
        }
        .is_empty());
    }
}
