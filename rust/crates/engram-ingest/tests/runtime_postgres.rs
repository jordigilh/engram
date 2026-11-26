//! End-to-end runtime harness: real Postgres ledger + mock Hindsight + a real
//! temporary docs root (issue #242).

mod support;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use chrono::{Duration as LedgerDuration, Utc};

use engram_ingest::config::RuntimeConfig;
use engram_ingest::runtime::{Runtime, SourceOutcome};
use engram_ingest::worker::Shutdown;
use engram_ingest::{claim_job, connect, count_drain_jobs, migrate};
use postgres::Client;
use support::TestServer;

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn test_url() -> Option<String> {
    std::env::var("ENGRAM_INGEST_TEST_DATABASE_URL")
        .ok()
        .filter(|value| !value.trim().is_empty())
}

fn temp_dir(label: &str) -> PathBuf {
    let unique = COUNTER.fetch_add(1, Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!(
        "engram-ingest-runtime-{label}-{}-{unique}",
        std::process::id()
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
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
    client
        .batch_execute("SET lock_timeout = '5s'")
        .expect("lock timeout");
    migrate(&mut client).expect("migrate");
    truncate(&mut client);
    Some((url, client))
}

fn write_doc(root: &Path, relative: &str, body: &str) {
    let path = root.join(relative);
    std::fs::create_dir_all(path.parent().expect("parent")).expect("dirs");
    std::fs::write(path, body).expect("document");
}

fn toml_escape(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

fn write_config(root: &Path, disorder: &Path, database_url: &str, hindsight_url: &str) -> PathBuf {
    let path = disorder.join("projects.toml");
    let body = format!(
        r#"
[defaults]
hindsight_url = "{}"
pg_dsn = "{}"
scan_interval_seconds = 1
concurrency = 1

[projects.example]
docs_bank = "example-docs"

[[projects.example.sources]]
tag = "repo"
root = "{}"
docs_include = ["**/*.md"]
docs_exclude = ["**/vendor/**"]
"#,
        toml_escape(hindsight_url),
        toml_escape(database_url),
        toml_escape(&root.to_string_lossy())
    );
    std::fs::write(&path, body).expect("config");
    path
}

fn load_config(path: &Path) -> RuntimeConfig {
    RuntimeConfig::load_with_env(Some(path), &["example".to_string()], &|_| None)
        .expect("runtime config")
}

fn count_jobs(client: &mut Client, state: &str) -> i64 {
    client
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs WHERE state = $1",
            &[&state],
        )
        .expect("job count")
        .get(0)
}

fn active_documents(client: &mut Client) -> i64 {
    client
        .query_one(
            "SELECT count(*) FROM engram_ingest.documents WHERE state = 'active'",
            &[],
        )
        .expect("document count")
        .get(0)
}

fn methods(requests: &[String]) -> Vec<String> {
    requests
        .iter()
        .filter_map(|request| request.split_whitespace().next().map(str::to_string))
        .collect()
}

fn run_and_drain(config: RuntimeConfig) {
    let runtime = Runtime::new(config).expect("runtime");
    let handle = runtime.start(Shutdown::default()).expect("start");
    handle.drain(Duration::from_secs(30)).expect("drain");
}

#[test]
fn ingests_retains_deletes_and_exposes_metrics() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("docs");
    let disorder = temp_dir("config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    write_doc(&root, "nested/b.md", "# B\n\nbeta content");
    write_doc(&root, "vendor/skip.md", "# Skip\n\nnot ingested");
    let config_path = write_config(&root, &disorder, &database_url, server.url());

    let runtime = Runtime::new(load_config(&config_path)).expect("runtime");
    let handle = runtime.start(Shutdown::default()).expect("start");
    assert!(matches!(
        handle.prepared().outcomes.as_slice(),
        [SourceOutcome::Applied {
            scanned_files: 2,
            ..
        }]
    ));
    let metrics = handle.metrics();
    assert!(metrics.contains("example-docs"), "metrics: {metrics}");
    assert!(!handle.health().is_empty());
    let _ = handle.startup_recovery();
    handle.drain(Duration::from_secs(30)).expect("drain");

    assert!(count_jobs(&mut client, "succeeded") >= 2);
    assert_eq!(active_documents(&mut client), 2);
    let requests = server.requests();
    assert!(
        methods(&requests)
            .iter()
            .filter(|method| *method == "POST")
            .count()
            >= 2,
        "requests: {requests:?}"
    );

    // A second pass over unchanged files must not enqueue another retain.
    let before_requests = requests.len();
    let before_jobs = count_jobs(&mut client, "succeeded");
    run_and_drain(load_config(&config_path));
    assert_eq!(
        server.requests().len(),
        before_requests,
        "no duplicate retain"
    );
    assert_eq!(count_jobs(&mut client, "succeeded"), before_jobs);

    // Deletion is reconciled into a Hindsight DELETE.
    std::fs::remove_file(root.join("a.md")).expect("remove");
    run_and_drain(load_config(&config_path));
    let requests = server.requests();
    assert!(
        methods(&requests).iter().any(|method| method == "DELETE"),
        "requests: {requests:?}"
    );
    assert_eq!(active_documents(&mut client), 1);
}

#[test]
fn restart_resumes_pending_jobs_without_duplicates() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("resume-docs");
    let disorder = temp_dir("resume-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    let config_path = write_config(&root, &disorder, &database_url, server.url());
    let config = load_config(&config_path);

    // First process scans and applies, then exits before a worker exists.
    let report = Runtime::new(config.clone())
        .expect("runtime")
        .prepare()
        .expect("prepare");
    assert!(matches!(
        report.outcomes.as_slice(),
        [SourceOutcome::Applied { .. }]
    ));
    let pending = count_jobs(&mut client, "pending");
    assert!(pending > 0, "prepare should enqueue retain jobs");
    assert!(server.requests().is_empty());

    // A fresh process starts, reconciles nothing new, and drains the old jobs.
    let before_posts = methods(&server.requests())
        .iter()
        .filter(|method| *method == "POST")
        .count();
    let handle = Runtime::new(config)
        .expect("runtime")
        .start(Shutdown::default())
        .expect("start");
    assert!(matches!(
        handle.prepared().outcomes.as_slice(),
        [SourceOutcome::Applied { summary, .. }] if summary.jobs_created == 0
    ));
    handle.drain(Duration::from_secs(30)).expect("drain");
    let after_posts = methods(&server.requests())
        .iter()
        .filter(|method| *method == "POST")
        .count();
    assert_eq!(
        (after_posts - before_posts) as i64,
        pending,
        "the restart must send exactly the pending retains"
    );
    assert_eq!(count_jobs(&mut client, "pending"), 0);
}

#[test]
fn run_exits_on_preexisting_shutdown_after_a_manual_scan() {
    let Some((database_url, _client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("shutdown-docs");
    let disorder = temp_dir("shutdown-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    let config_path = write_config(&root, &disorder, &database_url, server.url());
    let config = load_config(&config_path);

    let runtime = Runtime::new(config.clone()).expect("runtime");
    let outcomes = runtime.scan_once().expect("scan once");
    assert!(matches!(
        outcomes.as_slice(),
        [SourceOutcome::Applied { .. }]
    ));

    let shutdown = Shutdown::default();
    let handle = runtime.start(shutdown.clone()).expect("start");
    handle.shutdown();
    handle.run().expect("run exits cleanly");
}

#[test]
fn missing_root_does_not_tombstone_the_manifest() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("missing-docs");
    let disorder = temp_dir("missing-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    let config_path = write_config(&root, &disorder, &database_url, server.url());
    run_and_drain(load_config(&config_path));
    assert_eq!(active_documents(&mut client), 1);
    let requests = server.requests().len();

    std::fs::remove_dir_all(&root).expect("remove root");
    let runtime = Runtime::new(load_config(&config_path)).expect("runtime");
    let handle = runtime.start(Shutdown::default()).expect("start");
    assert!(matches!(
        handle.prepared().outcomes.as_slice(),
        [SourceOutcome::Skipped { .. }]
    ));
    handle.drain(Duration::from_secs(10)).expect("drain");

    assert_eq!(active_documents(&mut client), 1, "manifest must survive");
    assert_eq!(server.requests().len(), requests, "no delete must be sent");
}

#[test]
fn empty_root_requires_an_explicit_opt_in() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("empty-docs");
    let disorder = temp_dir("empty-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    let config_path = write_config(&root, &disorder, &database_url, server.url());
    run_and_drain(load_config(&config_path));
    assert_eq!(active_documents(&mut client), 1);

    std::fs::remove_file(root.join("a.md")).expect("empty root");
    let runtime = Runtime::new(load_config(&config_path)).expect("runtime");
    let handle = runtime.start(Shutdown::default()).expect("start");
    assert!(matches!(
        handle.prepared().outcomes.as_slice(),
        [SourceOutcome::Guarded { .. }]
    ));
    handle.drain(Duration::from_secs(10)).expect("drain");
    assert_eq!(active_documents(&mut client), 1, "guard keeps documents");

    let config = load_config(&config_path).with_allow_empty_root(true);
    let handle = Runtime::new(config)
        .expect("runtime")
        .start(Shutdown::default())
        .expect("start");
    handle.drain(Duration::from_secs(30)).expect("drain");
    assert_eq!(active_documents(&mut client), 0);
    let deletes: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs
              WHERE kind = 'delete' AND state = 'succeeded'",
            &[],
        )
        .expect("delete jobs")
        .get(0);
    assert!(deletes > 0, "opt-in must deliver the deletions");
}

#[test]
fn drain_counts_an_expired_lease_until_recovery() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("expired-docs");
    let disorder = temp_dir("expired-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    let config_path = write_config(&root, &disorder, &database_url, server.url());
    Runtime::new(load_config(&config_path))
        .expect("runtime")
        .prepare()
        .expect("prepare");

    let claimed = claim_job(
        &mut client,
        "example-docs",
        "expired-test",
        LedgerDuration::milliseconds(50),
        Utc::now(),
    )
    .expect("claim")
    .expect("queued job");
    std::thread::sleep(Duration::from_millis(100));
    let counts = count_drain_jobs(&mut client, &["example-docs".to_string()], Utc::now())
        .expect("drain counts");
    assert_eq!(counts.leased, 1, "expired leases are still undelivered");
    assert!(!counts.is_empty());

    // Recovery clears the lease and returns the job to the pending count.
    engram_ingest::recover_expired_leases_for_banks(
        &mut client,
        &["example-docs".to_string()],
        Utc::now(),
    )
    .expect("recover");
    let counts = count_drain_jobs(&mut client, &["example-docs".to_string()], Utc::now())
        .expect("drain counts");
    assert_eq!(counts.leased, 0);
    assert!(counts.pending >= 1);
    assert_eq!(counts.pending as i64, count_jobs(&mut client, "pending"));
    assert_eq!(claimed.kind, "retain");
}

#[cfg(unix)]
fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_engram-ingest")
}

#[cfg(unix)]
#[test]
fn cli_once_fails_when_a_root_is_guarded_or_missing() {
    let Some((database_url, _client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("cli-guard-docs");
    let disorder = temp_dir("cli-guard-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    let config_path = write_config(&root, &disorder, &database_url, server.url());
    run_and_drain(load_config(&config_path));

    let run_once = || {
        std::process::Command::new(binary())
            .args([
                "run",
                "--config",
                config_path.to_str().expect("config utf8"),
                "--project",
                "example",
                "--once",
                "--drain-timeout",
                "30",
            ])
            .env_remove("ENGRAM_PROJECTS_CONFIG")
            .env_remove("HINDSIGHT_URL")
            .env_remove("COCOINDEX_PG_URL")
            .output()
            .expect("run binary")
    };

    std::fs::remove_file(root.join("a.md")).expect("empty root");
    let output = run_once();
    assert!(!output.status.success(), "guarded root must fail --once");
    assert!(String::from_utf8_lossy(&output.stderr).contains("guarded"));

    std::fs::remove_dir_all(&root).expect("missing root");
    let output = run_once();
    assert!(!output.status.success(), "missing root must fail --once");
    assert!(String::from_utf8_lossy(&output.stderr).contains("skipped"));
}

#[cfg(unix)]
fn wait_for_exit(child: &mut std::process::Child, timeout: Duration) -> std::process::ExitStatus {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait().expect("wait") {
            return status;
        }
        if std::time::Instant::now() >= deadline {
            let _ = child.kill();
            panic!("child did not exit within {timeout:?}");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[cfg(unix)]
#[test]
fn cli_once_ingests_and_exits_zero() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("cli-once-docs");
    let disorder = temp_dir("cli-once-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    let config_path = write_config(&root, &disorder, &database_url, server.url());

    let output = std::process::Command::new(binary())
        .args([
            "run",
            "--config",
            config_path.to_str().expect("config utf8"),
            "--project",
            "example",
            "--once",
            "--drain-timeout",
            "30",
        ])
        .env_remove("ENGRAM_PROJECTS_CONFIG")
        .env_remove("HINDSIGHT_URL")
        .env_remove("COCOINDEX_PG_URL")
        .output()
        .expect("run binary");
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(count_jobs(&mut client, "succeeded") >= 1);
    assert!(!server.requests().is_empty());
}

#[cfg(unix)]
#[test]
fn cli_resident_exits_zero_on_sigterm() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("cli-sigterm-docs");
    let disorder = temp_dir("cli-sigterm-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    let config_path = write_config(&root, &disorder, &database_url, server.url());

    let mut child = std::process::Command::new(binary())
        .args([
            "run",
            "--config",
            config_path.to_str().expect("config utf8"),
            "--project",
            "example",
            "--scan-interval",
            "1",
        ])
        .env_remove("ENGRAM_PROJECTS_CONFIG")
        .env_remove("HINDSIGHT_URL")
        .env_remove("COCOINDEX_PG_URL")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .expect("spawn binary");

    let deadline = std::time::Instant::now() + Duration::from_secs(15);
    while server.requests().is_empty() && std::time::Instant::now() < deadline {
        if child.try_wait().expect("wait").is_some() {
            panic!("resident runtime exited before ingesting");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(
        !server.requests().is_empty(),
        "runtime never called Hindsight"
    );

    let status = std::process::Command::new("kill")
        .args(["-TERM", &child.id().to_string()])
        .status()
        .expect("send SIGTERM");
    assert!(status.success());
    let status = wait_for_exit(&mut child, Duration::from_secs(30));
    assert!(status.success(), "resident runtime failed on SIGTERM");
    let leased: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs WHERE state = 'leased'",
            &[],
        )
        .expect("leased count")
        .get(0);
    assert_eq!(leased, 0, "shutdown must drain leases");
}
