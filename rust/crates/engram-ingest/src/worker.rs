//! Bank-scoped ingestion worker loop (issue #214, worker-loop slice).
//!
//! Each bank gets a bounded set of slots. A slot owns one `postgres::Client`
//! and one clone of the shared `HindsightSink`; jobs are claimed with
//! `FOR UPDATE SKIP LOCKED` inside the bank namespace. The loop is synchronous
//! because the ledger and sink clients are blocking; cancellation is a
//! condition-variable flag, and DB statement timeouts bound the in-flight work.

use std::collections::BTreeMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration as StdDuration, Instant};

use chrono::{DateTime, Duration as LedgerDuration, Utc};
use postgres::{Client, NoTls};

use crate::ledger::is_valid_bank_id;
use crate::sink::{
    process_one_reporting, DeleteOutcome, HindsightSink, ProcessStep, SinkError, SinkOutcome,
};
use crate::store::{
    count_stranded_expired_leases, migrate, recover_expired_leases_in_bank,
    recover_expired_leases_outside_banks, RecoveryReport, StoreError, RECOVERY_BATCH,
};

/// Injected claim clock. Only `claim_job` uses it; acks, failures, and recovery
/// use the wall clock so a lost ack remains detectable in tests.
pub type Clock = Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>;

const MAX_SLOTS_PER_BANK: usize = 8;
const MAX_TOTAL_SLOTS: usize = 32;
const MAX_LEASE_SECONDS: i64 = 3_600;
/// Bound startup recovery so a huge backlog cannot block the listener forever.
const MAX_STARTUP_RECOVERY_BATCHES: usize = 20;

fn system_clock() -> Clock {
    Arc::new(Utc::now)
}

#[derive(Debug, thiserror::Error)]
pub enum WorkerError {
    #[error(transparent)]
    Ledger(#[from] StoreError),
    #[error("invalid worker configuration: {0}")]
    InvalidConfig(String),
    #[error("worker thread failed: {0}")]
    Thread(String),
    #[error("worker shutdown timed out after {0:?}")]
    JoinTimeout(StdDuration),
}

/// Exponential backoff with additive jitter capped at `max`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BackoffPolicy {
    pub base: StdDuration,
    pub max: StdDuration,
    pub jitter_fraction: f64,
}

impl BackoffPolicy {
    pub fn new(base: StdDuration, max: StdDuration, jitter_fraction: f64) -> Self {
        Self {
            base,
            max,
            jitter_fraction: jitter_fraction.clamp(0.0, 1.0),
        }
    }

    /// `fraction` is in `0.0..=1.0`; callers pass a random sample in production
    /// and a fixed value in tests.
    pub fn delay(&self, attempt: u32, fraction: f64) -> StdDuration {
        let factor = 1u32 << attempt.min(31);
        let exponential = self.base.saturating_mul(factor).min(self.max);
        let jitter = exponential.mul_f64(self.jitter_fraction * fraction.clamp(0.0, 1.0));
        (exponential + jitter).min(self.max)
    }
}

fn jitter_sample() -> f64 {
    let mut bytes = [0u8; 8];
    if getrandom::getrandom(&mut bytes).is_ok() {
        u64::from_le_bytes(bytes) as f64 / u64::MAX as f64
    } else {
        0.5
    }
}

/// Cancellation flag that also wakes sleeping slots immediately.
#[derive(Debug, Clone, Default)]
pub struct Shutdown {
    inner: Arc<ShutdownInner>,
}

#[derive(Debug, Default)]
struct ShutdownInner {
    cancelled: Mutex<bool>,
    condvar: Condvar,
}

impl Shutdown {
    fn lock(&self) -> MutexGuard<'_, bool> {
        self.inner
            .cancelled
            .lock()
            .unwrap_or_else(|error| error.into_inner())
    }

    pub fn cancel(&self) {
        let mut cancelled = self.lock();
        *cancelled = true;
        self.inner.condvar.notify_all();
    }

    pub fn is_cancelled(&self) -> bool {
        *self.lock()
    }

    /// Returns false when cancelled. A zero duration still checks the flag.
    pub fn sleep(&self, duration: StdDuration) -> bool {
        if duration.is_zero() {
            return !self.is_cancelled();
        }
        let cancelled = self.lock();
        if *cancelled {
            return false;
        }
        let (cancelled, _) = self
            .inner
            .condvar
            .wait_timeout_while(cancelled, duration, |cancelled| !*cancelled)
            .unwrap_or_else(|error| error.into_inner());
        !*cancelled
    }
}

/// Per-bank counters. Every counter has exactly one producer: slot outcomes
/// update job/outcome counters, runtime recovery folds in through `ProcessStep`,
/// and the supervisor records startup recovery on the handle.
fn escape_prometheus_label(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '\\' => escaped.push_str("\\\\"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str("\\n"),
            other => escaped.push(other),
        }
    }
    escaped
}

#[derive(Debug)]
pub struct WorkerMetrics {
    bank_id: String,
    claimed: AtomicU64,
    retained: AtomicU64,
    deleted: AtomicU64,
    already_absent: AtomicU64,
    retried: AtomicU64,
    dead: AtomicU64,
    lease_lost: AtomicU64,
    recovered: AtomicU64,
    recovery_dead: AtomicU64,
    worker_dead: AtomicU64,
    idle_polls: AtomicU64,
    ledger_errors: AtomicU64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkerMetricsSnapshot {
    pub bank_id: String,
    pub claimed: u64,
    pub retained: u64,
    pub deleted: u64,
    pub already_absent: u64,
    pub retried: u64,
    pub dead: u64,
    pub lease_lost: u64,
    pub recovered: u64,
    pub recovery_dead: u64,
    pub worker_dead: u64,
    pub idle_polls: u64,
    pub ledger_errors: u64,
}

impl WorkerMetrics {
    pub fn new(bank_id: &str) -> Self {
        Self {
            bank_id: bank_id.to_string(),
            claimed: AtomicU64::new(0),
            retained: AtomicU64::new(0),
            deleted: AtomicU64::new(0),
            already_absent: AtomicU64::new(0),
            retried: AtomicU64::new(0),
            dead: AtomicU64::new(0),
            lease_lost: AtomicU64::new(0),
            recovered: AtomicU64::new(0),
            recovery_dead: AtomicU64::new(0),
            worker_dead: AtomicU64::new(0),
            idle_polls: AtomicU64::new(0),
            ledger_errors: AtomicU64::new(0),
        }
    }

    pub fn bank_id(&self) -> &str {
        &self.bank_id
    }

    fn record_step(&self, step: &ProcessStep) {
        if let Some(job) = &step.job {
            self.claimed.fetch_add(1, Ordering::Relaxed);
            match &job.outcome {
                SinkOutcome::Retained => {
                    self.retained.fetch_add(1, Ordering::Relaxed);
                }
                SinkOutcome::Deleted(DeleteOutcome::Deleted) => {
                    self.deleted.fetch_add(1, Ordering::Relaxed);
                }
                SinkOutcome::Deleted(DeleteOutcome::AlreadyAbsent) => {
                    self.already_absent.fetch_add(1, Ordering::Relaxed);
                }
                SinkOutcome::Retried(_) => {
                    self.retried.fetch_add(1, Ordering::Relaxed);
                }
                SinkOutcome::Dead(_) => {
                    self.dead.fetch_add(1, Ordering::Relaxed);
                }
                SinkOutcome::LeaseLost => {
                    self.lease_lost.fetch_add(1, Ordering::Relaxed);
                }
            }
        }
        self.recovered
            .fetch_add(step.recovery.requeued as u64, Ordering::Relaxed);
        self.recovery_dead
            .fetch_add(step.recovery.dead as u64, Ordering::Relaxed);
    }

    pub fn record_startup_recovery(&self, report: RecoveryReport) {
        self.recovered
            .fetch_add(report.requeued as u64, Ordering::Relaxed);
        self.recovery_dead
            .fetch_add(report.dead as u64, Ordering::Relaxed);
    }

    fn record_idle_poll(&self) {
        self.idle_polls.fetch_add(1, Ordering::Relaxed);
    }

    fn record_ledger_error(&self) {
        self.ledger_errors.fetch_add(1, Ordering::Relaxed);
    }

    fn record_worker_dead(&self) {
        self.worker_dead.fetch_add(1, Ordering::Relaxed);
    }

    pub fn snapshot(&self) -> WorkerMetricsSnapshot {
        WorkerMetricsSnapshot {
            bank_id: self.bank_id.clone(),
            claimed: self.claimed.load(Ordering::Relaxed),
            retained: self.retained.load(Ordering::Relaxed),
            deleted: self.deleted.load(Ordering::Relaxed),
            already_absent: self.already_absent.load(Ordering::Relaxed),
            retried: self.retried.load(Ordering::Relaxed),
            dead: self.dead.load(Ordering::Relaxed),
            lease_lost: self.lease_lost.load(Ordering::Relaxed),
            recovered: self.recovered.load(Ordering::Relaxed),
            recovery_dead: self.recovery_dead.load(Ordering::Relaxed),
            worker_dead: self.worker_dead.load(Ordering::Relaxed),
            idle_polls: self.idle_polls.load(Ordering::Relaxed),
            ledger_errors: self.ledger_errors.load(Ordering::Relaxed),
        }
    }

    pub fn render_prometheus(&self) -> String {
        let snapshot = self.snapshot();
        let bank = escape_prometheus_label(&snapshot.bank_id);
        let mut output = String::new();
        for (name, value) in [
            ("engram_ingest_jobs_claimed_total", snapshot.claimed),
            ("engram_ingest_jobs_retained_total", snapshot.retained),
            ("engram_ingest_jobs_deleted_total", snapshot.deleted),
            (
                "engram_ingest_jobs_already_absent_total",
                snapshot.already_absent,
            ),
            ("engram_ingest_jobs_retried_total", snapshot.retried),
            ("engram_ingest_jobs_dead_total", snapshot.dead),
            ("engram_ingest_jobs_lease_lost_total", snapshot.lease_lost),
            ("engram_ingest_leases_recovered_total", snapshot.recovered),
            ("engram_ingest_recovery_dead_total", snapshot.recovery_dead),
            ("engram_ingest_worker_dead_total", snapshot.worker_dead),
            ("engram_ingest_idle_polls_total", snapshot.idle_polls),
            ("engram_ingest_ledger_errors_total", snapshot.ledger_errors),
        ] {
            output.push_str(&format!("{name}{{bank=\"{bank}\"}} {value}\n"));
        }
        output
    }
}

/// One step of a driver. Implementations must not panic on ordinary errors.
pub trait JobDriver: Send {
    fn step(&mut self) -> Result<ProcessStep, SinkError>;
    fn reconnect(&mut self) -> Result<(), StoreError>;
    /// Time until the next periodic recovery. `None` means no cadence cap.
    fn next_recovery_in(&self) -> Option<StdDuration>;
}

#[derive(Debug, Clone, Copy)]
struct DbTimeouts {
    connect: StdDuration,
    statement: StdDuration,
    lock: StdDuration,
}

fn connect_worker(url: &str, timeouts: DbTimeouts) -> Result<Client, StoreError> {
    let mut config: postgres::Config = url.parse()?;
    config.connect_timeout(timeouts.connect);
    let mut client = config.connect(NoTls)?;
    client.batch_execute(&format!(
        "SET statement_timeout = '{}ms'; SET lock_timeout = '{}ms'",
        timeouts.statement.as_millis(),
        timeouts.lock.as_millis()
    ))?;
    Ok(client)
}

struct LedgerDriver {
    url: String,
    db: Client,
    sink: HindsightSink,
    bank_id: String,
    owner: String,
    ttl: LedgerDuration,
    clock: Clock,
    recovery_interval: StdDuration,
    last_recovery: Option<Instant>,
    timeouts: DbTimeouts,
}

impl LedgerDriver {
    #[allow(clippy::too_many_arguments)]
    fn new(
        url: String,
        db: Client,
        sink: HindsightSink,
        bank_id: String,
        owner: String,
        ttl: LedgerDuration,
        clock: Clock,
        recovery_interval: StdDuration,
        timeouts: DbTimeouts,
    ) -> Self {
        Self {
            url,
            db,
            sink,
            bank_id,
            owner,
            ttl,
            clock,
            recovery_interval,
            last_recovery: None,
            timeouts,
        }
    }
}

impl JobDriver for LedgerDriver {
    fn step(&mut self) -> Result<ProcessStep, SinkError> {
        let now = (self.clock)();
        let recovery_due = self
            .last_recovery
            .is_none_or(|last| last.elapsed() >= self.recovery_interval);
        let step = process_one_reporting(
            &mut self.db,
            &self.sink,
            &self.bank_id,
            &self.owner,
            self.ttl,
            now,
            recovery_due,
        )?;
        if recovery_due {
            self.last_recovery = Some(Instant::now());
        }
        Ok(step)
    }

    fn reconnect(&mut self) -> Result<(), StoreError> {
        self.db = connect_worker(&self.url, self.timeouts)?;
        self.last_recovery = None;
        Ok(())
    }

    fn next_recovery_in(&self) -> Option<StdDuration> {
        self.last_recovery.map(|last| {
            self.recovery_interval
                .saturating_sub(last.elapsed())
                .max(StdDuration::from_millis(1))
        })
    }
}

fn run_loop(
    driver: &mut dyn JobDriver,
    metrics: &WorkerMetrics,
    shutdown: &Shutdown,
    idle_backoff: BackoffPolicy,
    error_backoff: BackoffPolicy,
) {
    let mut idle_attempt = 0u32;
    let mut error_attempt = 0u32;
    while !shutdown.is_cancelled() {
        let sleep = match driver.step() {
            Ok(step) => {
                metrics.record_step(&step);
                if step.recovery_error.is_some() {
                    metrics.record_ledger_error();
                    // The recovery transaction rolled back; keep the cadence and
                    // do not churn a new connection for a failed recovery.
                }
                error_attempt = 0;
                if step.job.is_some() {
                    // The ledger already owns retry backoff; a retried job must
                    // not idle this slot while other jobs are claimable.
                    idle_attempt = 0;
                    continue;
                }
                metrics.record_idle_poll();
                let delay = idle_backoff.delay(idle_attempt, jitter_sample());
                idle_attempt = idle_attempt.saturating_add(1);
                (delay, true)
            }
            Err(_) => {
                metrics.record_ledger_error();
                if !shutdown.is_cancelled() {
                    let _ = driver.reconnect();
                }
                let delay = error_backoff.delay(error_attempt, jitter_sample());
                error_attempt = error_attempt.saturating_add(1);
                // Do not cap an error backoff with the recovery timer: a broken
                // database would otherwise be retried every few milliseconds.
                (delay, false)
            }
        };
        let (sleep, cap_recovery) = sleep;
        let sleep = if cap_recovery {
            match driver.next_recovery_in() {
                Some(remaining) => sleep.min(remaining.max(StdDuration::from_millis(1))),
                None => sleep,
            }
        } else {
            sleep
        };
        if !shutdown.sleep(sleep) {
            break;
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BankConfig {
    pub bank_id: String,
    pub concurrency: usize,
}

pub struct WorkerConfig {
    pub database_url: String,
    pub sink: HindsightSink,
    pub banks: Vec<BankConfig>,
    pub lease_ttl: LedgerDuration,
    pub idle_backoff: BackoffPolicy,
    pub error_backoff: BackoffPolicy,
    pub recovery_interval: StdDuration,
    pub owner_prefix: String,
    pub connect_timeout: StdDuration,
    pub statement_timeout: StdDuration,
    pub lock_timeout: StdDuration,
    pub clock: Clock,
}

impl WorkerConfig {
    pub fn new(
        database_url: impl Into<String>,
        sink: HindsightSink,
        banks: Vec<BankConfig>,
    ) -> Self {
        let lease_ttl = sink.minimum_ttl();
        Self {
            database_url: database_url.into(),
            sink,
            banks,
            lease_ttl,
            idle_backoff: BackoffPolicy::new(
                StdDuration::from_millis(500),
                StdDuration::from_secs(5),
                0.2,
            ),
            error_backoff: BackoffPolicy::new(
                StdDuration::from_secs(1),
                StdDuration::from_secs(30),
                0.2,
            ),
            recovery_interval: StdDuration::from_secs(5),
            owner_prefix: "engram-ingest".to_string(),
            connect_timeout: StdDuration::from_secs(5),
            statement_timeout: StdDuration::from_secs(30),
            lock_timeout: StdDuration::from_secs(5),
            clock: system_clock(),
        }
    }

    fn timeouts(&self) -> DbTimeouts {
        DbTimeouts {
            connect: self.connect_timeout,
            statement: self.statement_timeout,
            lock: self.lock_timeout,
        }
    }

    fn validate(&self) -> Result<(), WorkerError> {
        if self.banks.is_empty() {
            return Err(WorkerError::InvalidConfig(
                "at least one bank is required".to_string(),
            ));
        }
        if self.owner_prefix.trim().is_empty() {
            return Err(WorkerError::InvalidConfig(
                "owner_prefix must not be empty".to_string(),
            ));
        }
        let mut seen = BTreeMap::new();
        let mut total = 0usize;
        for bank in &self.banks {
            if !is_valid_bank_id(&bank.bank_id) {
                return Err(WorkerError::InvalidConfig(format!(
                    "bank id {:?} is not path-segment safe",
                    bank.bank_id
                )));
            }
            if bank.concurrency == 0 || bank.concurrency > MAX_SLOTS_PER_BANK {
                return Err(WorkerError::InvalidConfig(format!(
                    "bank {} concurrency must be 1..={MAX_SLOTS_PER_BANK}",
                    bank.bank_id
                )));
            }
            if seen.insert(bank.bank_id.clone(), ()).is_some() {
                return Err(WorkerError::InvalidConfig(format!(
                    "bank {} is configured more than once",
                    bank.bank_id
                )));
            }
            total += bank.concurrency;
        }
        if total > MAX_TOTAL_SLOTS {
            return Err(WorkerError::InvalidConfig(format!(
                "total worker slots {total} exceed {MAX_TOTAL_SLOTS}"
            )));
        }
        if self.lease_ttl < self.sink.minimum_ttl()
            || self.lease_ttl > LedgerDuration::seconds(MAX_LEASE_SECONDS)
        {
            return Err(WorkerError::InvalidConfig(format!(
                "lease ttl must be between {} and {MAX_LEASE_SECONDS}s",
                self.sink.minimum_ttl()
            )));
        }
        if self.connect_timeout.is_zero()
            || self.statement_timeout.as_millis() == 0
            || self.lock_timeout.as_millis() == 0
        {
            return Err(WorkerError::InvalidConfig(
                "database timeouts must be at least 1ms".to_string(),
            ));
        }
        if !(0.0..=1.0).contains(&self.idle_backoff.jitter_fraction)
            || !(0.0..=1.0).contains(&self.error_backoff.jitter_fraction)
        {
            return Err(WorkerError::InvalidConfig(
                "backoff jitter fractions must be within 0.0..=1.0".to_string(),
            ));
        }
        if self.recovery_interval.is_zero()
            || self.idle_backoff.base.is_zero()
            || self.error_backoff.base.is_zero()
            || self.idle_backoff.base > self.idle_backoff.max
            || self.error_backoff.base > self.error_backoff.max
            || self.idle_backoff.max.is_zero()
            || self.error_backoff.max.is_zero()
        {
            return Err(WorkerError::InvalidConfig(
                "recovery interval and backoff bases must be positive, and base must not exceed max"
                    .to_string(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SlotState {
    Running,
    Cancelled,
    Exited,
    Panicked(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlotHealth {
    pub bank_id: String,
    pub slot: usize,
    pub state: SlotState,
}

#[derive(Debug, Clone)]
struct SlotStatus(Arc<Mutex<SlotState>>);

impl SlotStatus {
    fn new() -> Self {
        Self(Arc::new(Mutex::new(SlotState::Running)))
    }

    fn set(&self, state: SlotState) {
        *self.0.lock().unwrap_or_else(|error| error.into_inner()) = state;
    }

    fn get(&self) -> SlotState {
        self.0
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }
}

struct ThreadSet {
    handles: Vec<(String, usize, SlotStatus, JoinHandle<()>)>,
}

impl ThreadSet {
    fn join_timeout(mut self, timeout: StdDuration) -> Result<(), WorkerError> {
        let deadline = Instant::now() + timeout;
        while self
            .handles
            .iter()
            .any(|(_, _, _, handle)| !handle.is_finished())
        {
            if Instant::now() >= deadline {
                // Deliberately detach: the caller gets a bounded shutdown and
                // the leases expire into TTL recovery.
                return Err(WorkerError::JoinTimeout(timeout));
            }
            thread::sleep(StdDuration::from_millis(10));
        }
        for (_, _, _, handle) in self.handles.drain(..) {
            let _ = handle.join();
        }
        Ok(())
    }

    fn health(&self) -> Vec<SlotHealth> {
        self.handles
            .iter()
            .map(|(bank_id, slot, status, _)| SlotHealth {
                bank_id: bank_id.clone(),
                slot: *slot,
                state: status.get(),
            })
            .collect()
    }
}

pub struct WorkerHandle {
    shutdown: Shutdown,
    threads: Option<ThreadSet>,
    metrics: Vec<Arc<WorkerMetrics>>,
    startup_recovery: RecoveryReport,
    stranded_remaining: u64,
    startup_recovery_failed: bool,
    default_join_timeout: StdDuration,
}

impl Drop for WorkerHandle {
    fn drop(&mut self) {
        // A dropped handle must not leave slots running invisibly.
        self.shutdown.cancel();
    }
}

impl WorkerHandle {
    pub fn shutdown(&self) {
        self.shutdown.cancel();
    }

    /// Cancel and wait for all slots using the startup bound: the longest sink
    /// request plus four statement timeouts and a 5 s margin. This is a
    /// best-effort bound; callers with unusually large recovery batches should
    /// use [`join_timeout`](Self::join_timeout) with their own deadline.
    pub fn join(self) -> Result<(), WorkerError> {
        let timeout = self.default_join_timeout;
        self.join_timeout(timeout)
    }

    /// Cancel and wait up to `timeout`. On timeout the threads are detached.
    pub fn join_timeout(mut self, timeout: StdDuration) -> Result<(), WorkerError> {
        self.shutdown.cancel();
        let threads = self.threads.take().expect("worker threads are present");
        threads.join_timeout(timeout)
    }

    pub fn snapshot(&self) -> Vec<WorkerMetricsSnapshot> {
        self.metrics
            .iter()
            .map(|metric| metric.snapshot())
            .collect()
    }

    pub fn prometheus(&self) -> String {
        const TYPES: &str = "\
# TYPE engram_ingest_jobs_claimed_total counter
# TYPE engram_ingest_jobs_retained_total counter
# TYPE engram_ingest_jobs_deleted_total counter
# TYPE engram_ingest_jobs_already_absent_total counter
# TYPE engram_ingest_jobs_retried_total counter
# TYPE engram_ingest_jobs_dead_total counter
# TYPE engram_ingest_jobs_lease_lost_total counter
# TYPE engram_ingest_leases_recovered_total counter
# TYPE engram_ingest_recovery_dead_total counter
# TYPE engram_ingest_worker_dead_total counter
# TYPE engram_ingest_idle_polls_total counter
# TYPE engram_ingest_ledger_errors_total counter
# TYPE engram_ingest_recovery_stranded_at_startup gauge
# TYPE engram_ingest_recovery_stranded_requeued gauge
# TYPE engram_ingest_startup_recovery_failed gauge
";
        let mut output = String::from(TYPES);
        for metric in &self.metrics {
            output.push_str(&metric.render_prometheus());
        }
        output.push_str(&format!(
            "engram_ingest_recovery_stranded_at_startup {}\n",
            self.stranded_remaining
        ));
        output.push_str(&format!(
            "engram_ingest_recovery_stranded_requeued {}\n",
            self.startup_recovery.stranded
        ));
        output.push_str(&format!(
            "engram_ingest_startup_recovery_failed {}\n",
            u8::from(self.startup_recovery_failed)
        ));
        output
    }

    pub fn startup_recovery(&self) -> RecoveryReport {
        self.startup_recovery
    }

    pub fn stranded_remaining(&self) -> u64 {
        self.stranded_remaining
    }

    pub fn startup_recovery_failed(&self) -> bool {
        self.startup_recovery_failed
    }

    pub fn health(&self) -> Vec<SlotHealth> {
        self.threads
            .as_ref()
            .map(ThreadSet::health)
            .unwrap_or_default()
    }
}

pub struct WorkerSupervisor;

impl WorkerSupervisor {
    pub fn start(config: WorkerConfig) -> Result<WorkerHandle, WorkerError> {
        config.validate()?;
        let timeouts = config.timeouts();
        let join_timeout = config.sink.max_request_timeout()
            + config.statement_timeout.saturating_mul(4)
            + StdDuration::from_secs(5);

        // Schema readiness is fail-loud: a v2 ledger with no explicit bank must
        // stop startup, not silently strand jobs with a NULL bank_id.
        let mut admin = connect_worker(&config.database_url, timeouts)?;
        migrate(&mut admin)?;
        // migrate() disables statement_timeout for DDL; restore the worker's
        // bounds before startup recovery runs on the same connection.
        admin
            .batch_execute(&format!(
                "SET statement_timeout = '{}ms'; SET lock_timeout = '{}ms'",
                timeouts.statement.as_millis(),
                timeouts.lock.as_millis()
            ))
            .map_err(StoreError::Postgres)?;
        let bank_ids: Vec<String> = config
            .banks
            .iter()
            .map(|bank| bank.bank_id.clone())
            .collect();

        let mut metrics_by_bank = BTreeMap::new();
        for bank in &config.banks {
            metrics_by_bank.insert(
                bank.bank_id.clone(),
                Arc::new(WorkerMetrics::new(&bank.bank_id)),
            );
        }

        // Attribute configured-bank recovery to its own metrics, then catch
        // leases for banks that are no longer configured (stranded). Each pass
        // is bounded so an extreme backlog cannot block worker startup. A
        // recovery error is recorded but does not prevent serving banks.
        let mut startup_recovery = RecoveryReport::default();
        let mut startup_recovery_failed = false;
        let mut startup_batches = 0usize;
        'configured: for bank_id in &bank_ids {
            while startup_batches < MAX_STARTUP_RECOVERY_BATCHES {
                startup_batches += 1;
                let report = match recover_expired_leases_in_bank(&mut admin, bank_id, Utc::now()) {
                    Ok(report) => report,
                    Err(_) => {
                        startup_recovery_failed = true;
                        if let Some(metrics) = metrics_by_bank.get(bank_id) {
                            metrics.record_ledger_error();
                        }
                        break 'configured;
                    }
                };
                let done = report.scanned < RECOVERY_BATCH;
                if let Some(metrics) = metrics_by_bank.get(bank_id) {
                    metrics.record_startup_recovery(report);
                }
                startup_recovery.merge(report);
                if done {
                    break;
                }
            }
        }
        while startup_batches < MAX_STARTUP_RECOVERY_BATCHES {
            startup_batches += 1;
            let report =
                match recover_expired_leases_outside_banks(&mut admin, &bank_ids, Utc::now()) {
                    Ok(report) => report,
                    Err(_) => {
                        startup_recovery_failed = true;
                        break;
                    }
                };
            let done = report.scanned < RECOVERY_BATCH;
            startup_recovery.merge(report);
            if done {
                break;
            }
        }
        let stranded_remaining =
            match count_stranded_expired_leases(&mut admin, &bank_ids, Utc::now()) {
                Ok(count) => count as u64,
                Err(_) => {
                    startup_recovery_failed = true;
                    0
                }
            };
        drop(admin);

        // Connect every slot before spawning so a connection failure cannot
        // leave a partially started worker set behind.
        let mut slots = Vec::new();
        for bank in &config.banks {
            for slot in 0..bank.concurrency {
                let client = connect_worker(&config.database_url, timeouts)?;
                let owner = format!(
                    "{}-{}-{}-{}",
                    config.owner_prefix,
                    std::process::id(),
                    bank.bank_id,
                    slot
                );
                slots.push((
                    bank.bank_id.clone(),
                    slot,
                    LedgerDriver::new(
                        config.database_url.clone(),
                        client,
                        config.sink.clone(),
                        bank.bank_id.clone(),
                        owner,
                        config.lease_ttl,
                        config.clock.clone(),
                        config.recovery_interval,
                        timeouts,
                    ),
                    SlotStatus::new(),
                ));
            }
        }

        let shutdown = Shutdown::default();
        let mut handles = Vec::new();
        for (bank_id, slot, mut driver, status) in slots {
            let metrics = Arc::clone(
                metrics_by_bank
                    .get(&bank_id)
                    .expect("configured bank has metrics"),
            );
            let thread_shutdown = shutdown.clone();
            let thread_status = status.clone();
            let idle = config.idle_backoff;
            let error = config.error_backoff;
            let name = format!("engram-ingest-{bank_id}-{slot}");
            let spawned = thread::Builder::new().name(name).spawn(move || {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    run_loop(&mut driver, &metrics, &thread_shutdown, idle, error);
                }));
                match result {
                    Ok(()) if thread_shutdown.is_cancelled() => {
                        thread_status.set(SlotState::Cancelled);
                    }
                    Ok(()) => {
                        metrics.record_worker_dead();
                        thread_status.set(SlotState::Exited);
                    }
                    Err(payload) => {
                        metrics.record_worker_dead();
                        let message = payload
                            .downcast_ref::<&str>()
                            .map(|message| (*message).to_string())
                            .or_else(|| payload.downcast_ref::<String>().cloned())
                            .unwrap_or_else(|| "unknown panic".to_string());
                        thread_status.set(SlotState::Panicked(message));
                    }
                }
            });
            match spawned {
                Ok(handle) => handles.push((bank_id, slot, status, handle)),
                Err(error) => {
                    shutdown.cancel();
                    let started = ThreadSet { handles };
                    let _ = started.join_timeout(StdDuration::from_secs(5));
                    return Err(WorkerError::Thread(error.to_string()));
                }
            }
        }

        let metrics = config
            .banks
            .iter()
            .map(|bank| {
                Arc::clone(
                    metrics_by_bank
                        .get(&bank.bank_id)
                        .expect("configured bank has metrics"),
                )
            })
            .collect();
        Ok(WorkerHandle {
            shutdown,
            threads: Some(ThreadSet { handles }),
            metrics,
            startup_recovery,
            stranded_remaining,
            startup_recovery_failed,
            default_join_timeout: join_timeout,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sink::ProcessedJob;
    use std::collections::VecDeque;
    use std::sync::atomic::AtomicUsize;

    fn retained(job_id: i64) -> ProcessStep {
        ProcessStep {
            job: Some(ProcessedJob {
                job_id,
                kind: "retain".to_string(),
                outcome: SinkOutcome::Retained,
            }),
            recovery: RecoveryReport::default(),
            recovery_error: None,
        }
    }

    fn retried(job_id: i64) -> ProcessStep {
        ProcessStep {
            job: Some(ProcessedJob {
                job_id,
                kind: "retain".to_string(),
                outcome: SinkOutcome::Retried("temporary".to_string()),
            }),
            recovery: RecoveryReport::default(),
            recovery_error: None,
        }
    }

    #[test]
    fn backoff_grows_caps_and_jitters() {
        let policy = BackoffPolicy::new(
            StdDuration::from_millis(10),
            StdDuration::from_millis(100),
            0.5,
        );
        assert_eq!(policy.delay(0, 0.0), StdDuration::from_millis(10));
        assert_eq!(policy.delay(1, 0.0), StdDuration::from_millis(20));
        assert_eq!(policy.delay(2, 0.0), StdDuration::from_millis(40));
        assert_eq!(policy.delay(10, 0.0), StdDuration::from_millis(100));
        assert_eq!(policy.delay(10, 1.0), StdDuration::from_millis(100));
        assert_eq!(policy.delay(0, 1.0), StdDuration::from_millis(15));
    }

    #[test]
    fn shutdown_wakes_a_sleeping_slot() {
        let shutdown = Shutdown::default();
        let sleeper = shutdown.clone();
        let started = Instant::now();
        let handle = thread::spawn(move || {
            thread::sleep(StdDuration::from_millis(20));
            sleeper.cancel();
        });
        assert!(!shutdown.sleep(StdDuration::from_secs(30)));
        assert!(started.elapsed() < StdDuration::from_secs(1));
        handle.join().unwrap();
        assert!(!shutdown.sleep(StdDuration::from_millis(1)));
    }

    #[test]
    fn metrics_count_outcomes_and_recovery() {
        let metrics = WorkerMetrics::new("repo-docs");
        metrics.record_step(&retained(1));
        metrics.record_step(&ProcessStep {
            job: Some(ProcessedJob {
                job_id: 2,
                kind: "retain".to_string(),
                outcome: SinkOutcome::Dead("permanent".to_string()),
            }),
            recovery: RecoveryReport {
                requeued: 1,
                dead: 2,
                stranded: 3,
                scanned: 4,
            },
            recovery_error: None,
        });
        metrics.record_idle_poll();
        metrics.record_ledger_error();
        metrics.record_worker_dead();
        let snapshot = metrics.snapshot();
        assert_eq!(snapshot.bank_id, "repo-docs");
        assert_eq!(snapshot.claimed, 2);
        assert_eq!(snapshot.retained, 1);
        assert_eq!(snapshot.dead, 1);
        assert_eq!(snapshot.recovered, 1);
        assert_eq!(snapshot.recovery_dead, 2);
        assert_eq!(snapshot.idle_polls, 1);
        assert_eq!(snapshot.ledger_errors, 1);
        assert_eq!(snapshot.worker_dead, 1);
        let rendered = metrics.render_prometheus();
        assert!(rendered.contains("engram_ingest_jobs_retained_total{bank=\"repo-docs\"} 1"));
    }

    struct FakeDriver {
        steps: VecDeque<Result<ProcessStep, SinkError>>,
        shutdown: Shutdown,
        cancel_after: Option<usize>,
        calls: Arc<AtomicUsize>,
        reconnects: Arc<AtomicUsize>,
        recovery_hint: Option<StdDuration>,
    }

    impl JobDriver for FakeDriver {
        fn step(&mut self) -> Result<ProcessStep, SinkError> {
            self.calls.fetch_add(1, Ordering::Relaxed);
            if self
                .cancel_after
                .is_some_and(|limit| self.calls.load(Ordering::Relaxed) >= limit)
            {
                self.shutdown.cancel();
            }
            self.steps
                .pop_front()
                .unwrap_or_else(|| Ok(ProcessStep::default()))
        }

        fn reconnect(&mut self) -> Result<(), StoreError> {
            self.reconnects.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }

        fn next_recovery_in(&self) -> Option<StdDuration> {
            self.recovery_hint
        }
    }

    #[test]
    fn scripted_loop_processes_retries_and_empty_polls() {
        let shutdown = Shutdown::default();
        let metrics = WorkerMetrics::new("repo-docs");
        let calls = Arc::new(AtomicUsize::new(0));
        let reconnects = Arc::new(AtomicUsize::new(0));
        let mut driver = FakeDriver {
            steps: VecDeque::from([
                Ok(retained(1)),
                Err(SinkError::Transient("ledger".to_string())),
                Ok(ProcessStep::default()),
            ]),
            shutdown: shutdown.clone(),
            // Four calls: retained, error, empty, then cancel before sleeping.
            cancel_after: Some(3),
            calls: Arc::clone(&calls),
            reconnects: Arc::clone(&reconnects),
            recovery_hint: None,
        };
        let started = Instant::now();
        run_loop(
            &mut driver,
            &metrics,
            &shutdown,
            BackoffPolicy::new(StdDuration::from_secs(10), StdDuration::from_secs(10), 0.0),
            BackoffPolicy::new(
                StdDuration::from_millis(10),
                StdDuration::from_millis(10),
                0.0,
            ),
        );
        assert!(started.elapsed() < StdDuration::from_secs(1));
        assert_eq!(calls.load(Ordering::Relaxed), 3);
        assert_eq!(reconnects.load(Ordering::Relaxed), 1);
        let snapshot = metrics.snapshot();
        assert_eq!(snapshot.retained, 1);
        assert_eq!(snapshot.idle_polls, 1);
        assert_eq!(snapshot.ledger_errors, 1);
    }

    #[test]
    fn retried_job_does_not_sleep_the_slot() {
        let shutdown = Shutdown::default();
        let metrics = WorkerMetrics::new("repo-docs");
        let calls = Arc::new(AtomicUsize::new(0));
        let mut driver = FakeDriver {
            steps: VecDeque::from([Ok(retried(1)), Ok(retained(2))]),
            shutdown: shutdown.clone(),
            cancel_after: Some(2),
            calls: Arc::clone(&calls),
            reconnects: Arc::new(AtomicUsize::new(0)),
            recovery_hint: None,
        };
        let started = Instant::now();
        run_loop(
            &mut driver,
            &metrics,
            &shutdown,
            BackoffPolicy::new(StdDuration::from_secs(10), StdDuration::from_secs(10), 0.0),
            BackoffPolicy::new(StdDuration::from_secs(10), StdDuration::from_secs(10), 0.0),
        );
        assert!(started.elapsed() < StdDuration::from_secs(1));
        assert_eq!(calls.load(Ordering::Relaxed), 2);
        assert_eq!(metrics.snapshot().retried, 1);
        assert_eq!(metrics.snapshot().retained, 1);
    }

    fn test_sink() -> HindsightSink {
        HindsightSink::new("http://127.0.0.1:1").expect("sink")
    }

    #[test]
    fn config_validation_rejects_bad_banks_and_budgets() {
        let config = WorkerConfig::new(
            "postgresql://example/db",
            test_sink(),
            vec![BankConfig {
                bank_id: "repo-docs".to_string(),
                concurrency: 1,
            }],
        );
        assert!(config.validate().is_ok());

        let mut duplicate = WorkerConfig::new(
            "postgresql://example/db",
            test_sink(),
            vec![
                BankConfig {
                    bank_id: "repo-docs".to_string(),
                    concurrency: 1,
                },
                BankConfig {
                    bank_id: "repo-docs".to_string(),
                    concurrency: 1,
                },
            ],
        );
        assert!(matches!(
            duplicate.validate(),
            Err(WorkerError::InvalidConfig(_))
        ));

        let zero = WorkerConfig::new(
            "postgresql://example/db",
            test_sink(),
            vec![BankConfig {
                bank_id: "repo-docs".to_string(),
                concurrency: 0,
            }],
        );
        assert!(zero.validate().is_err());

        let unsafe_bank = WorkerConfig::new(
            "postgresql://example/db",
            test_sink(),
            vec![BankConfig {
                bank_id: "bad/bank".to_string(),
                concurrency: 1,
            }],
        );
        assert!(unsafe_bank.validate().is_err());

        duplicate.banks.clear();
        duplicate.banks.push(BankConfig {
            bank_id: "repo-docs".to_string(),
            concurrency: MAX_SLOTS_PER_BANK,
        });
        duplicate.banks.push(BankConfig {
            bank_id: "repo-issues".to_string(),
            concurrency: MAX_SLOTS_PER_BANK,
        });
        duplicate.banks.push(BankConfig {
            bank_id: "repo-code".to_string(),
            concurrency: MAX_SLOTS_PER_BANK,
        });
        duplicate.banks.push(BankConfig {
            bank_id: "repo-extra".to_string(),
            concurrency: MAX_SLOTS_PER_BANK,
        });
        duplicate.banks.push(BankConfig {
            bank_id: "repo-more".to_string(),
            concurrency: 1,
        });
        assert!(duplicate.validate().is_err());

        let short_ttl = WorkerConfig {
            lease_ttl: LedgerDuration::seconds(1),
            ..WorkerConfig::new(
                "postgresql://example/db",
                test_sink(),
                vec![BankConfig {
                    bank_id: "repo-docs".to_string(),
                    concurrency: 1,
                }],
            )
        };
        assert!(short_ttl.validate().is_err());
        let _ = zero.banks.len();
        let _ = unsafe_bank.banks.len();
    }

    #[test]
    fn config_validation_rejects_nan_jitter_and_submillisecond_timeouts() {
        let mut config = WorkerConfig::new(
            "postgresql://example/db",
            test_sink(),
            vec![BankConfig {
                bank_id: "repo-docs".to_string(),
                concurrency: 1,
            }],
        );
        config.idle_backoff.jitter_fraction = f64::NAN;
        assert!(config.validate().is_err());

        let mut config = WorkerConfig::new(
            "postgresql://example/db",
            test_sink(),
            vec![BankConfig {
                bank_id: "repo-docs".to_string(),
                concurrency: 1,
            }],
        );
        config.statement_timeout = StdDuration::from_micros(500);
        assert!(config.validate().is_err());
    }

    #[test]
    fn prometheus_escapes_the_bank_label() {
        let metrics = WorkerMetrics::new("repo\"docs\n\\");
        let rendered = metrics.render_prometheus();
        assert!(
            rendered.contains("bank=\"repo\\\"docs\\n\\\\\""),
            "{rendered}"
        );
    }

    #[test]
    fn thread_set_join_timeout_and_health() {
        let status = SlotStatus::new();
        let handle = thread::spawn(|| thread::sleep(StdDuration::from_millis(200)));
        let set = ThreadSet {
            handles: vec![("repo-docs".to_string(), 0, status, handle)],
        };
        assert!(matches!(
            set.join_timeout(StdDuration::from_millis(10)),
            Err(WorkerError::JoinTimeout(_))
        ));

        let status = SlotStatus::new();
        status.set(SlotState::Panicked("boom".to_string()));
        let handle = thread::spawn(|| {});
        let set = ThreadSet {
            handles: vec![("repo-docs".to_string(), 1, status, handle)],
        };
        assert_eq!(
            set.health(),
            vec![SlotHealth {
                bank_id: "repo-docs".to_string(),
                slot: 1,
                state: SlotState::Panicked("boom".to_string()),
            }]
        );
        set.join_timeout(StdDuration::from_millis(100)).unwrap();
    }
}
