//! End-to-end runtime harness: real Postgres ledger + mock Hindsight + a real
//! temporary docs root (issue #242).

mod support;

use chrono::{Duration as LedgerDuration, Utc};
use std::ops::Deref;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use engram_ingest::config::RuntimeConfig;
use engram_ingest::runtime::{Runtime, RuntimeError, SourceOutcome};
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

struct TempDir(PathBuf);

impl Deref for TempDir {
    type Target = Path;

    fn deref(&self) -> &Path {
        &self.0
    }
}

impl TempDir {
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn temp_dir(label: &str) -> TempDir {
    let unique = COUNTER.fetch_add(1, Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!(
        "engram-ingest-runtime-{label}-{}-{unique}",
        std::process::id()
    ));
    std::fs::create_dir(&path).expect("temp dir");
    TempDir(path)
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
    write_config_maybe_transcripts(Some(root), None, disorder, database_url, hindsight_url)
}

fn write_config_maybe_transcripts(
    root: Option<&Path>,
    transcripts_root: Option<&Path>,
    disorder: &Path,
    database_url: &str,
    hindsight_url: &str,
) -> PathBuf {
    let docs = root
        .map(|root| {
            format!(
                r#"
[[projects.example.sources]]
tag = "repo"
root = "{}"
docs_include = ["**/*.md"]
docs_exclude = ["**/vendor/**"]
"#,
                toml_escape(&root.to_string_lossy())
            )
        })
        .unwrap_or_default();
    let transcripts = transcripts_root
        .map(|root| {
            format!(
                r#"transcripts = true
transcript_bank = "example-transcripts"
workspace_prefixes = ["workspace-"]

[projects.example.paths]
transcripts_dir = "{}"
"#,
                toml_escape(&root.to_string_lossy())
            )
        })
        .unwrap_or_default();
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
{}{}"#,
        toml_escape(hindsight_url),
        toml_escape(database_url),
        transcripts,
        docs
    );
    std::fs::write(&path, body).expect("config");
    path
}

fn write_transcript(root: &Path, name: &str, messages: &[serde_json::Value]) {
    let dir = root.join("workspace-demo").join("agent-transcripts");
    std::fs::create_dir_all(&dir).expect("transcript dir");
    let body = messages
        .iter()
        .map(|message| serde_json::to_string(message).unwrap())
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(dir.join(format!("{name}.jsonl")), body + "\n").expect("transcript");
}

fn transcript_user(text: &str) -> serde_json::Value {
    serde_json::json!({"role": "user", "message": {"content": text}})
}

fn transcript_assistant(text: &str) -> serde_json::Value {
    serde_json::json!({"role": "assistant", "message": {"content": text}})
}

fn transcript_bank_posts(server: &TestServer) -> usize {
    server
        .requests()
        .iter()
        .filter(|request| {
            request.starts_with("POST") && request.contains("banks/example-transcripts/")
        })
        .count()
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
fn cancelled_once_drain_fails_when_work_was_observed() {
    let Some((database_url, _client)) = setup() else {
        return;
    };
    let server = TestServer::start(500, Duration::ZERO);
    let root = temp_dir("cancel-docs");
    let disorder = temp_dir("cancel-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    let config_path = write_config(&root, &disorder, &database_url, server.url());

    let handle = Runtime::new(load_config(&config_path))
        .expect("runtime")
        .start(Shutdown::default())
        .expect("start");
    handle.shutdown();
    let result = handle.drain(Duration::from_secs(10));
    let cancelled = matches!(&result, Err(RuntimeError::DrainCancelled { .. }));
    assert!(
        cancelled,
        "cancelled drain must report the observed backlog: {result:?}"
    );
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

    std::fs::remove_dir_all(root.path()).expect("remove root");
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

#[test]
fn transcripts_ingest_retain_and_ack_end_to_end() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let root = temp_dir("mixed-docs");
    let transcripts = temp_dir("mixed-transcripts");
    let disorder = temp_dir("mixed-config");
    write_doc(&root, "a.md", "# A\n\nalpha content");
    write_transcript(
        &transcripts,
        "session",
        &[
            transcript_assistant("I will use the first approach."),
            transcript_user("No, that's wrong; use the other approach."),
            transcript_assistant("Understood, switching to the other approach."),
        ],
    );
    let config_path = write_config_maybe_transcripts(
        Some(&root),
        Some(&transcripts),
        &disorder,
        &database_url,
        server.url(),
    );

    let runtime = Runtime::new(load_config(&config_path)).expect("runtime");
    let handle = runtime.start(Shutdown::default()).expect("start");
    assert!(matches!(
        handle.prepared().outcomes.as_slice(),
        [
            SourceOutcome::Applied { .. },
            SourceOutcome::Transcripts {
                files: 1,
                windows_retained: 1,
                windows_queued: 0,
                ..
            }
        ]
    ));
    // The transcript bank is served by the same worker supervisor.
    let metrics = handle.metrics();
    assert!(metrics.contains("example-docs"), "metrics: {metrics}");
    assert!(
        metrics.contains("example-transcripts"),
        "metrics: {metrics}"
    );
    handle.drain(Duration::from_secs(30)).expect("drain");

    let posts = transcript_bank_posts(&server);
    assert_eq!(posts, 1, "one window must reach the transcript bank");
    assert!(count_jobs(&mut client, "succeeded") >= 2);

    // A second pass after the ack must not re-retain the same window.
    run_and_drain(load_config(&config_path));
    assert_eq!(
        transcript_bank_posts(&server),
        posts,
        "acked watermark must suppress replay"
    );
}

#[test]
fn transcripts_only_project_ingests_without_docs_sources() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let transcripts = temp_dir("only-transcripts");
    let disorder = temp_dir("only-config");
    write_transcript(
        &transcripts,
        "session",
        &[
            transcript_user("No, that's wrong; use the other approach."),
            transcript_assistant("Understood, switching to the other approach."),
        ],
    );
    let config_path = write_config_maybe_transcripts(
        None,
        Some(&transcripts),
        &disorder,
        &database_url,
        server.url(),
    );

    let runtime = Runtime::new(load_config(&config_path)).expect("runtime");
    let handle = runtime.start(Shutdown::default()).expect("start");
    assert!(matches!(
        handle.prepared().outcomes.as_slice(),
        [SourceOutcome::Transcripts {
            files: 1,
            windows_retained: 1,
            ..
        }]
    ));
    assert_eq!(handle.bank_ids(), ["example-transcripts"]);
    handle.drain(Duration::from_secs(30)).expect("drain");
    assert_eq!(transcript_bank_posts(&server), 1);
    assert!(count_jobs(&mut client, "succeeded") >= 1);
}

#[test]
fn missing_transcript_root_is_skipped_without_tombstoning() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let transcripts = temp_dir("missing-transcripts");
    let disorder = temp_dir("missing-transcripts-config");
    write_transcript(
        &transcripts,
        "session",
        &[transcript_user("No, that's wrong; use the other approach.")],
    );
    let config_path = write_config_maybe_transcripts(
        None,
        Some(&transcripts),
        &disorder,
        &database_url,
        server.url(),
    );
    run_and_drain(load_config(&config_path));
    let posts = transcript_bank_posts(&server);
    assert_eq!(posts, 1);
    let active: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.chunks WHERE state = 'active'",
            &[],
        )
        .expect("chunks")
        .get(0);
    assert_eq!(active, 1);

    std::fs::remove_dir_all(transcripts.path()).expect("remove transcript root");
    let runtime = Runtime::new(load_config(&config_path)).expect("runtime");
    let handle = runtime.start(Shutdown::default()).expect("start");
    assert!(matches!(
        handle.prepared().outcomes.as_slice(),
        [SourceOutcome::Skipped { .. }]
    ));
    handle.drain(Duration::from_secs(10)).expect("drain");
    assert_eq!(transcript_bank_posts(&server), posts, "no delete may fire");
    let active: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.chunks WHERE state = 'active'",
            &[],
        )
        .expect("chunks")
        .get(0);
    assert_eq!(active, 1, "the manifest must survive a missing root");
}

#[test]
fn appended_transcript_messages_ingest_only_new_windows() {
    let Some((database_url, _client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let transcripts = temp_dir("append-transcripts");
    let disorder = temp_dir("append-config");
    write_transcript(
        &transcripts,
        "session",
        &[
            transcript_assistant("I will use the first approach."),
            transcript_user("No, that's wrong; use the other approach."),
        ],
    );
    let config_path = write_config_maybe_transcripts(
        None,
        Some(&transcripts),
        &disorder,
        &database_url,
        server.url(),
    );
    run_and_drain(load_config(&config_path));
    assert_eq!(transcript_bank_posts(&server), 1);

    write_transcript(
        &transcripts,
        "session",
        &[
            transcript_assistant("I will use the first approach."),
            transcript_user("No, that's wrong; use the other approach."),
            transcript_assistant("Understood, switching to the other approach."),
            transcript_user("Don't do that; always run the tests before committing."),
            transcript_assistant("Understood, tests first."),
        ],
    );
    run_and_drain(load_config(&config_path));
    assert_eq!(
        transcript_bank_posts(&server),
        2,
        "only the newly appended signal may be retained"
    );
}

#[test]
fn prefix_typo_does_not_mass_tombstone() {
    let Some((database_url, mut client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let transcripts = temp_dir("typo-transcripts");
    let disorder = temp_dir("typo-config");
    write_transcript(
        &transcripts,
        "session",
        &[transcript_user("No, that's wrong; use the other approach.")],
    );
    let config_path = write_config_maybe_transcripts(
        None,
        Some(&transcripts),
        &disorder,
        &database_url,
        server.url(),
    );
    run_and_drain(load_config(&config_path));
    let posts = transcript_bank_posts(&server);
    assert_eq!(posts, 1);
    let active: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.chunks WHERE state = 'active'",
            &[],
        )
        .expect("chunks")
        .get(0);
    assert_eq!(active, 1);

    // Rewrite the config with a prefix that matches nothing: the root still
    // exists and discovery is complete, so the guard must fire instead of
    // tombstoning every active window.
    let typo_path = disorder.join("typo.toml");
    std::fs::write(
        &typo_path,
        format!(
            r#"
[defaults]
hindsight_url = "{}"
pg_dsn = "{}"
scan_interval_seconds = 1
concurrency = 1

[projects.example]
transcripts = true
transcript_bank = "example-transcripts"
workspace_prefixes = ["workspace-typo-"]

[projects.example.paths]
transcripts_dir = "{}"
"#,
            toml_escape(server.url()),
            toml_escape(&database_url),
            toml_escape(&transcripts.to_string_lossy())
        ),
    )
    .expect("typo config");

    let runtime = Runtime::new(load_config(&typo_path)).expect("runtime");
    let handle = runtime.start(Shutdown::default()).expect("start");
    assert!(matches!(
        handle.prepared().outcomes.as_slice(),
        [SourceOutcome::Guarded { .. }]
    ));
    handle.drain(Duration::from_secs(10)).expect("drain");
    assert_eq!(transcript_bank_posts(&server), posts, "no delete may fire");
    let active: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.chunks WHERE state = 'active'",
            &[],
        )
        .expect("chunks")
        .get(0);
    assert_eq!(active, 1, "the guard must keep every window");

    // The explicit opt-in treats the empty discovery as a real deletion.
    let config = load_config(&typo_path).with_allow_empty_root(true);
    let handle = Runtime::new(config)
        .expect("runtime")
        .start(Shutdown::default())
        .expect("start");
    assert!(matches!(
        handle.prepared().outcomes.as_slice(),
        [SourceOutcome::Transcripts { .. }]
    ));
    handle.drain(Duration::from_secs(30)).expect("drain");
    let requests = server.requests();
    assert!(
        methods(&requests).iter().any(|method| *method == "DELETE"),
        "the opt-in must deliver the deletions: {requests:?}"
    );
    let active: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.chunks WHERE state = 'active'",
            &[],
        )
        .expect("chunks")
        .get(0);
    assert_eq!(active, 0);
}

#[cfg(unix)]
#[test]
fn unreadable_transcript_root_is_skipped() {
    let Some((database_url, _client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let transcripts = temp_dir("unreadable-transcripts");
    let disorder = temp_dir("unreadable-config");
    write_transcript(
        &transcripts,
        "session",
        &[transcript_user("No, that's wrong; use the other approach.")],
    );
    let config_path = write_config_maybe_transcripts(
        None,
        Some(&transcripts),
        &disorder,
        &database_url,
        server.url(),
    );

    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(transcripts.path(), std::fs::Permissions::from_mode(0o000))
        .expect("restrict root");
    // A root user bypasses directory permissions; skip the assertion rather
    // than report a false failure.
    let restricted = std::fs::read_dir(transcripts.path()).is_err();
    let result = std::panic::catch_unwind(|| {
        let runtime = Runtime::new(load_config(&config_path)).expect("runtime");
        let handle = runtime.start(Shutdown::default()).expect("start");
        let skipped = matches!(
            handle.prepared().outcomes.as_slice(),
            [SourceOutcome::Skipped { reason, .. }] if reason.contains("aborted")
        );
        handle.drain(Duration::from_secs(10)).expect("drain");
        skipped
    });
    std::fs::set_permissions(transcripts.path(), std::fs::Permissions::from_mode(0o755))
        .expect("restore root");
    assert!(
        result.is_ok(),
        "runtime must not panic on an unreadable root"
    );
    if restricted {
        assert!(
            result.unwrap(),
            "an unreadable root must be reported as skipped, not fatal"
        );
    }
}

#[cfg(unix)]
#[test]
fn cli_once_fails_when_the_transcript_root_is_missing() {
    let Some((database_url, _client)) = setup() else {
        return;
    };
    let server = TestServer::start(200, Duration::ZERO);
    let transcripts = temp_dir("cli-missing-transcripts");
    let disorder = temp_dir("cli-missing-config");
    write_transcript(
        &transcripts,
        "session",
        &[transcript_user("No, that's wrong; use the other approach.")],
    );
    let config_path = write_config_maybe_transcripts(
        None,
        Some(&transcripts),
        &disorder,
        &database_url,
        server.url(),
    );
    run_and_drain(load_config(&config_path));

    std::fs::remove_dir_all(transcripts.path()).expect("remove transcript root");
    let config = config_path.to_str().expect("config utf8");
    let (status, _stdout, stderr) = run_cli(
        &[
            "run",
            "--config",
            config,
            "--project",
            "example",
            "--once",
            "--drain-timeout",
            "30",
        ],
        Duration::from_secs(30),
    );
    assert!(
        !status.success(),
        "missing transcript root must fail --once"
    );
    assert!(stderr.contains("skipped"), "stderr: {stderr}");
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
        let config = config_path.to_str().expect("config utf8");
        run_cli(
            &[
                "run",
                "--config",
                config,
                "--project",
                "example",
                "--once",
                "--drain-timeout",
                "30",
            ],
            Duration::from_secs(30),
        )
    };

    std::fs::remove_file(root.join("a.md")).expect("empty root");
    let (status, _stdout, stderr) = run_once();
    assert!(!status.success(), "guarded root must fail --once");
    assert!(stderr.contains("guarded"));

    std::fs::remove_dir_all(root.path()).expect("missing root");
    let (status, _stdout, stderr) = run_once();
    assert!(!status.success(), "missing root must fail --once");
    assert!(stderr.contains("skipped"));
}

#[cfg(unix)]
struct ChildGuard(Option<std::process::Child>);

#[cfg(unix)]
impl ChildGuard {
    fn new(child: std::process::Child) -> Self {
        Self(Some(child))
    }

    fn child(&mut self) -> &mut std::process::Child {
        self.0.as_mut().expect("child")
    }

    fn disarm(mut self) {
        if let Some(mut child) = self.0.take() {
            let _ = child.wait();
        }
    }
}

#[cfg(unix)]
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if let Some(mut child) = self.0.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
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
            let _ = child.wait();
            panic!("child did not exit within {timeout:?}");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Run the CLI with a parent-enforced deadline so a stalled child cannot hold
/// the harness database lock forever.
#[cfg(unix)]
fn run_cli(args: &[&str], timeout: Duration) -> (std::process::ExitStatus, String, String) {
    use std::io::Read as _;
    let child = std::process::Command::new(binary())
        .args(args)
        .env_remove("ENGRAM_PROJECTS_CONFIG")
        .env_remove("HINDSIGHT_URL")
        .env_remove("COCOINDEX_PG_URL")
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("spawn binary");
    let mut guard = ChildGuard::new(child);
    let status = wait_for_exit(guard.child(), timeout);
    let mut stdout = String::new();
    let mut stderr = String::new();
    if let Some(mut pipe) = guard.child().stdout.take() {
        let _ = pipe.read_to_string(&mut stdout);
    }
    if let Some(mut pipe) = guard.child().stderr.take() {
        let _ = pipe.read_to_string(&mut stderr);
    }
    guard.disarm();
    (status, stdout, stderr)
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

    let config = config_path.to_str().expect("config utf8");
    let (status, _stdout, stderr) = run_cli(
        &[
            "run",
            "--config",
            config,
            "--project",
            "example",
            "--once",
            "--drain-timeout",
            "30",
        ],
        Duration::from_secs(30),
    );
    assert!(status.success(), "stderr: {stderr}");
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

    let child = std::process::Command::new(binary())
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
    let mut guard = ChildGuard::new(child);

    let deadline = std::time::Instant::now() + Duration::from_secs(15);
    while server.requests().is_empty() && std::time::Instant::now() < deadline {
        if guard.child().try_wait().expect("wait").is_some() {
            panic!("resident runtime exited before ingesting");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(
        !server.requests().is_empty(),
        "runtime never called Hindsight"
    );

    let status = std::process::Command::new("kill")
        .args(["-TERM", &guard.child().id().to_string()])
        .status()
        .expect("send SIGTERM");
    assert!(status.success());
    let status = wait_for_exit(guard.child(), Duration::from_secs(30));
    guard.disarm();
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
