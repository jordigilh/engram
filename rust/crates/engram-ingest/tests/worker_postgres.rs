//! Worker-loop Postgres + HTTP harness.
//!
//! Requires `ENGRAM_INGEST_TEST_DATABASE_URL` pointing at the disposable
//! `engram_ingest_test` database. Every test takes the same advisory lock as
//! the other ledger harnesses so `cargo test` binaries cannot truncate each
//! other's rows.

mod support;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration as StdDuration, Instant};

use chrono::{Duration as LedgerDuration, Utc};
use engram_ingest::{
    apply_reconciliation, claim_job, connect, load_manifest, migrate, plan_document, reconcile,
    upsert_source, BankConfig, ContentHash, HindsightSink, ScannedFile, SourceRecord, WorkerConfig,
    WorkerHandle, WorkerSupervisor, LAYOUT_VERSION,
};
use postgres::Client;

fn test_url() -> Option<String> {
    std::env::var("ENGRAM_INGEST_TEST_DATABASE_URL")
        .ok()
        .filter(|value| !value.trim().is_empty())
}

fn scanned(path: &str, content: &str) -> ScannedFile {
    ScannedFile {
        relative_path: path.to_string(),
        content_sha256: ContentHash::of(content.as_bytes()),
        size_bytes: content.len() as i64,
        calls: plan_document("repo", path, content, None).expect("plan"),
    }
}

fn truncate(client: &mut Client) {
    client
        .batch_execute(
            "TRUNCATE engram_ingest.leases, engram_ingest.jobs,
                      engram_ingest.tombstones, engram_ingest.chunks,
                      engram_ingest.documents, engram_ingest.sources
             RESTART IDENTITY CASCADE",
        )
        .expect("truncate");
}

fn setup() -> Option<(String, Client)> {
    let url = test_url()?;
    let mut client = connect(&url).expect("connect");
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .expect("database")
        .get(0);
    assert_eq!(database, "engram_ingest_test");
    client
        .execute("SELECT pg_advisory_lock($1)", &[&918_273_i64])
        .expect("harness lock");
    // Only bound migration/DDL locks; the harness lock above must not time out.
    client
        .batch_execute("SET lock_timeout = '5s'")
        .expect("lock timeout");
    migrate(&mut client).expect("migrate");
    truncate(&mut client);
    Some((url, client))
}

fn seed_source(
    client: &mut Client,
    project: &str,
    bank: &str,
    root: &str,
    files: &[(&str, &str)],
) -> i64 {
    let source = upsert_source(
        client,
        &SourceRecord::markdown(project, "repo", root, bank, &["**/*.md"]),
    )
    .expect("source");
    let scan: Vec<ScannedFile> = files
        .iter()
        .map(|(path, body)| scanned(path, body))
        .collect();
    let manifest = load_manifest(client, source).expect("manifest");
    let plan = reconcile(&manifest, &scan, LAYOUT_VERSION);
    apply_reconciliation(client, source, &plan, Utc::now()).expect("apply");
    source
}

fn wait_until<F>(client: &mut Client, timeout: StdDuration, mut check: F)
where
    F: FnMut(&mut Client) -> bool,
{
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if check(client) {
            return;
        }
        std::thread::sleep(StdDuration::from_millis(20));
    }
    panic!("condition not met within {timeout:?}");
}

fn succeeded_jobs(client: &mut Client) -> i64 {
    client
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs WHERE state = 'succeeded'",
            &[],
        )
        .expect("job count")
        .get(0)
}

fn wait_for_metrics<F>(worker: &RunningWorker, timeout: StdDuration, mut check: F)
where
    F: FnMut(&[engram_ingest::WorkerMetricsSnapshot]) -> bool,
{
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if check(&worker.handle().snapshot()) {
            return;
        }
        std::thread::sleep(StdDuration::from_millis(20));
    }
    panic!("metrics condition not met within {timeout:?}");
}

struct RunningWorker(Option<WorkerHandle>);

impl RunningWorker {
    fn new(handle: WorkerHandle) -> Self {
        Self(Some(handle))
    }

    fn handle(&self) -> &WorkerHandle {
        self.0.as_ref().expect("worker handle")
    }

    fn stop(mut self) {
        if let Some(handle) = self.0.take() {
            handle.shutdown();
            handle
                .join_timeout(StdDuration::from_secs(10))
                .expect("join");
        }
    }
}

impl Drop for RunningWorker {
    fn drop(&mut self) {
        if let Some(handle) = self.0.take() {
            handle.shutdown();
            let _ = handle.join_timeout(StdDuration::from_secs(10));
        }
    }
}

fn worker_config(url: &str, sink_url: &str, banks: Vec<BankConfig>) -> WorkerConfig {
    let sink = HindsightSink::new(sink_url).expect("sink");
    let mut config = WorkerConfig::new(url, sink, banks);
    config.idle_backoff = engram_ingest::BackoffPolicy::new(
        StdDuration::from_millis(20),
        StdDuration::from_millis(100),
        0.0,
    );
    config.error_backoff = engram_ingest::BackoffPolicy::new(
        StdDuration::from_millis(20),
        StdDuration::from_millis(100),
        0.0,
    );
    config.recovery_interval = StdDuration::from_millis(100);
    config.owner_prefix = "worker-harness".to_string();
    config
}

#[test]
fn banks_drain_independently_with_bounded_concurrency() {
    let Some((url, mut client)) = setup() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping worker harness");
        return;
    };
    let mut server = support::TestServer::start(200, StdDuration::from_millis(150));

    seed_source(
        &mut client,
        "project-a",
        "project-a-docs",
        "/tmp/wa",
        &[
            ("a1.md", "# A1\n\nAlpha one body long enough."),
            ("a2.md", "# A2\n\nAlpha two body long enough."),
            ("a3.md", "# A3\n\nAlpha three body long enough."),
        ],
    );
    seed_source(
        &mut client,
        "project-b",
        "project-b-docs",
        "/tmp/wb",
        &[
            ("b1.md", "# B1\n\nBeta one body long enough."),
            ("b2.md", "# B2\n\nBeta two body long enough."),
        ],
    );

    let config = worker_config(
        &url,
        server.url(),
        vec![
            BankConfig {
                bank_id: "project-a-docs".to_string(),
                concurrency: 2,
            },
            BankConfig {
                bank_id: "project-b-docs".to_string(),
                concurrency: 1,
            },
        ],
    );
    let worker = RunningWorker::new(WorkerSupervisor::start(config).expect("start"));

    wait_until(&mut client, StdDuration::from_secs(15), |client| {
        succeeded_jobs(client) == 5
    });
    wait_for_metrics(&worker, StdDuration::from_secs(5), |snapshots| {
        let bank_a = snapshots
            .iter()
            .find(|snapshot| snapshot.bank_id == "project-a-docs");
        let bank_b = snapshots
            .iter()
            .find(|snapshot| snapshot.bank_id == "project-b-docs");
        bank_a.is_some_and(|snapshot| snapshot.retained == 3)
            && bank_b.is_some_and(|snapshot| snapshot.retained == 2)
    });
    let snapshots = worker.handle().snapshot();
    let bank_a = snapshots
        .iter()
        .find(|snapshot| snapshot.bank_id == "project-a-docs")
        .expect("bank a metrics");
    let bank_b = snapshots
        .iter()
        .find(|snapshot| snapshot.bank_id == "project-b-docs")
        .expect("bank b metrics");
    assert_eq!(bank_a.retained, 3);
    assert_eq!(bank_b.retained, 2);

    assert!(
        server.max_in_flight("project-a-docs") <= 2,
        "bank A exceeded its slot bound: {}",
        server.max_in_flight("project-a-docs")
    );
    assert!(
        server.max_in_flight("project-b-docs") <= 1,
        "bank B exceeded its slot bound: {}",
        server.max_in_flight("project-b-docs")
    );
    assert!(server.max_in_flight("project-a-docs") >= 1);
    assert!(
        !worker.handle().health().is_empty(),
        "health must expose every slot"
    );

    worker.stop();

    // Shutdown is real: a job queued afterwards stays pending.
    seed_source(
        &mut client,
        "project-a",
        "project-a-docs",
        "/tmp/wa-late",
        &[("late.md", "# Late\n\nLate body long enough.")],
    );
    wait_until(&mut client, StdDuration::from_secs(5), |client| {
        let pending: i64 = client
            .query_one(
                "SELECT count(*) FROM engram_ingest.jobs WHERE state = 'pending'",
                &[],
            )
            .expect("pending count")
            .get(0);
        pending == 1
    });
    std::thread::sleep(StdDuration::from_millis(300));
    let still_pending: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs WHERE state = 'pending'",
            &[],
        )
        .expect("pending count")
        .get(0);
    assert_eq!(still_pending, 1, "no slot may survive shutdown");

    server.shutdown();
    truncate(&mut client);
}

#[test]
fn startup_recovery_paginates_a_large_stranded_backlog() {
    let Some((url, mut client)) = setup() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping worker harness");
        return;
    };
    let mut server = support::TestServer::start(200, StdDuration::ZERO);
    let orphan = seed_source(&mut client, "orphan", "orphan-docs", "/tmp/orphan", &[]);
    client
        .execute(
            "INSERT INTO engram_ingest.jobs
                 (source_id, bank_id, kind, hindsight_document_id, dedupe_key,
                  generation, payload, state, run_after)
             SELECT $1, 'orphan-docs', 'retain', 'orphan--' || g, 'orphan-' || g,
                    1, '{}'::jsonb, 'leased', now()
               FROM generate_series(1, 501) AS g",
            &[&orphan],
        )
        .expect("seed stranded jobs");
    client
        .batch_execute(
            "INSERT INTO engram_ingest.leases
                 (job_id, owner, token, expires_at, heartbeat_at)
             SELECT id, 'dead-worker', 'token-' || id,
                    now() - interval '1 hour', now()
               FROM engram_ingest.jobs
              WHERE bank_id = 'orphan-docs'",
        )
        .expect("seed stranded leases");

    let config = worker_config(
        &url,
        server.url(),
        vec![BankConfig {
            bank_id: "project-a-docs".to_string(),
            concurrency: 1,
        }],
    );
    let worker = RunningWorker::new(WorkerSupervisor::start(config).expect("start"));
    assert_eq!(worker.handle().startup_recovery().stranded, 501);
    assert_eq!(worker.handle().stranded_remaining(), 0);
    assert!(!worker.handle().startup_recovery_failed());
    let (leased, pending, attempts): (i64, i64, i64) = {
        let row = client
            .query_one(
                "SELECT
                     count(*) FILTER (WHERE state = 'leased'),
                     count(*) FILTER (WHERE state = 'pending'),
                     COALESCE(sum(attempts), 0)
                   FROM engram_ingest.jobs
                  WHERE bank_id = 'orphan-docs'",
                &[],
            )
            .expect("backlog counts");
        (row.get(0), row.get(1), row.get(2))
    };
    assert_eq!(leased, 0);
    assert_eq!(pending, 501);
    assert_eq!(attempts, 0);

    worker.stop();
    server.shutdown();
    truncate(&mut client);
}

#[test]
fn startup_recovery_handles_configured_and_deconfigured_banks() {
    let Some((url, mut client)) = setup() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping worker harness");
        return;
    };
    let mut server = support::TestServer::start(200, StdDuration::ZERO);
    seed_source(
        &mut client,
        "project-a",
        "project-a-docs",
        "/tmp/ra",
        &[("a.md", "# A\n\nAlpha body long enough.")],
    );
    seed_source(
        &mut client,
        "project-b",
        "project-b-docs",
        "/tmp/rb",
        &[("b.md", "# B\n\nBeta body long enough.")],
    );

    let expired = Utc::now() - LedgerDuration::seconds(10);
    let claimed_a = claim_job(
        &mut client,
        "project-a-docs",
        "dead-a",
        LedgerDuration::seconds(3600),
        Utc::now(),
    )
    .expect("claim a")
    .expect("a job");
    let claimed_b = claim_job(
        &mut client,
        "project-b-docs",
        "dead-b",
        LedgerDuration::seconds(3600),
        Utc::now(),
    )
    .expect("claim b")
    .expect("b job");
    client
        .execute(
            "UPDATE engram_ingest.leases SET expires_at = $1 WHERE job_id = ANY($2)",
            &[&expired, &vec![claimed_a.job_id, claimed_b.job_id]],
        )
        .expect("expire leases");

    let config = worker_config(
        &url,
        server.url(),
        vec![BankConfig {
            bank_id: "project-a-docs".to_string(),
            concurrency: 1,
        }],
    );
    let worker = RunningWorker::new(WorkerSupervisor::start(config).expect("start"));
    assert_eq!(worker.handle().startup_recovery().requeued, 1);
    assert_eq!(worker.handle().startup_recovery().stranded, 1);
    let startup_snapshot = worker.handle().snapshot();
    let startup_a = startup_snapshot
        .iter()
        .find(|snapshot| snapshot.bank_id == "project-a-docs")
        .expect("startup metrics");
    assert_eq!(startup_a.recovered, 1);

    wait_until(&mut client, StdDuration::from_secs(10), |client| {
        let state: String = client
            .query_one(
                "SELECT state FROM engram_ingest.jobs WHERE id = $1",
                &[&claimed_a.job_id],
            )
            .expect("a state")
            .get(0);
        state == "succeeded"
    });
    let (b_state, b_attempts): (String, i32) = {
        let row = client
            .query_one(
                "SELECT state, attempts FROM engram_ingest.jobs WHERE id = $1",
                &[&claimed_b.job_id],
            )
            .expect("b row");
        (row.get(0), row.get(1))
    };
    assert_eq!(b_state, "pending");
    assert_eq!(b_attempts, 0, "deconfigured banks must not burn attempts");
    assert!(
        server
            .requests()
            .iter()
            .all(|request| request.contains("project-a-docs")),
        "only the configured bank may call Hindsight"
    );

    worker.stop();
    server.shutdown();
    truncate(&mut client);
}

#[test]
fn runtime_recovery_wakes_an_idle_bank() {
    let Some((url, mut client)) = setup() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping worker harness");
        return;
    };
    let mut server = support::TestServer::start(200, StdDuration::ZERO);
    seed_source(
        &mut client,
        "project-a",
        "project-a-docs",
        "/tmp/idle",
        &[("idle.md", "# Idle\n\nIdle body long enough.")],
    );
    let claimed = claim_job(
        &mut client,
        "project-a-docs",
        "about-to-die",
        LedgerDuration::seconds(3_600),
        Utc::now(),
    )
    .expect("claim")
    .expect("job");

    let config = worker_config(
        &url,
        server.url(),
        vec![BankConfig {
            bank_id: "project-a-docs".to_string(),
            concurrency: 1,
        }],
    );
    let worker = RunningWorker::new(WorkerSupervisor::start(config).expect("start"));
    assert_eq!(worker.handle().startup_recovery().requeued, 0);
    // Expire only after startup so this exercises runtime recovery, not the
    // startup pass.
    client
        .execute(
            "UPDATE engram_ingest.leases
                SET expires_at = $1
              WHERE job_id = $2",
            &[&(Utc::now() - LedgerDuration::seconds(10)), &claimed.job_id],
        )
        .expect("expire lease after startup");

    wait_until(&mut client, StdDuration::from_secs(10), |client| {
        let state: String = client
            .query_one(
                "SELECT state FROM engram_ingest.jobs WHERE id = $1",
                &[&claimed.job_id],
            )
            .expect("state")
            .get(0);
        state == "succeeded"
    });
    let attempts: i32 = client
        .query_one(
            "SELECT attempts FROM engram_ingest.jobs WHERE id = $1",
            &[&claimed.job_id],
        )
        .expect("attempts")
        .get(0);
    assert_eq!(attempts, 1, "runtime recovery must consume one attempt");

    worker.stop();
    server.shutdown();
    truncate(&mut client);
}

#[test]
fn lost_ack_is_observable_and_consumes_one_attempt() {
    let Some((url, mut client)) = setup() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping worker harness");
        return;
    };
    let hook_client = Arc::new(Mutex::new(connect(&url).expect("hook db")));
    let expired = Arc::new(AtomicBool::new(false));
    let hook = Arc::new(move || {
        if expired.swap(true, Ordering::SeqCst) {
            return;
        }
        let mut hook_client = hook_client.lock().expect("hook db lock");
        hook_client
            .execute(
                "UPDATE engram_ingest.leases SET expires_at = $1",
                &[&(Utc::now() - LedgerDuration::seconds(10))],
            )
            .expect("expire lease during HTTP");
    });
    let mut server = support::TestServer::start_with_hook(200, StdDuration::ZERO, Some(hook));
    seed_source(
        &mut client,
        "project-a",
        "project-a-docs",
        "/tmp/lost",
        &[("lost.md", "# Lost\n\nLost body long enough.")],
    );

    let config = worker_config(
        &url,
        server.url(),
        vec![BankConfig {
            bank_id: "project-a-docs".to_string(),
            concurrency: 1,
        }],
    );
    let worker = RunningWorker::new(WorkerSupervisor::start(config).expect("start"));

    wait_until(&mut client, StdDuration::from_secs(5), |client| {
        let attempts: i64 = client
            .query_one(
                "SELECT count(*) FROM engram_ingest.jobs
                  WHERE state = 'pending' AND attempts = 1",
                &[],
            )
            .expect("attempts")
            .get(0);
        attempts == 1
    });
    wait_for_metrics(&worker, StdDuration::from_secs(5), |snapshots| {
        snapshots
            .iter()
            .find(|snapshot| snapshot.bank_id == "project-a-docs")
            .is_some_and(|snapshot| snapshot.lease_lost == 1 && snapshot.claimed == 1)
    });
    let snapshot = worker.handle().snapshot();
    let bank = snapshot
        .iter()
        .find(|snapshot| snapshot.bank_id == "project-a-docs")
        .expect("bank metrics");
    assert_eq!(bank.lease_lost, 1);
    assert_eq!(bank.claimed, 1);
    let state: String = client
        .query_one("SELECT state FROM engram_ingest.jobs", &[])
        .expect("state")
        .get(0);
    assert_eq!(state, "pending");

    worker.stop();
    server.shutdown();
    truncate(&mut client);
}
