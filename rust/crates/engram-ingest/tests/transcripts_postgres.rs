//! Postgres harness for native transcript watermarks and replay (issue #245).

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use chrono::{Duration, Utc};
use engram_ingest::{
    claim_job, complete_job, connect, fail_job, ingest_transcript_source,
    load_transcript_watermark, migrate, ContradictionMode, ContradictionResolution,
    ContradictionResolver, MemoryInvalidator, ResolutionAction, TranscriptOptions,
    TranscriptSource,
};
use postgres::Client;
use serde_json::json;

fn test_url() -> Option<String> {
    std::env::var("ENGRAM_INGEST_TEST_DATABASE_URL")
        .ok()
        .filter(|value| !value.trim().is_empty())
}

fn truncate(client: &mut Client) {
    client
        .batch_execute(
            "TRUNCATE engram_ingest.leases, engram_ingest.jobs,
                      engram_ingest.tombstones, engram_ingest.chunks,
                      engram_ingest.documents, engram_ingest.transcript_windows,
                      engram_ingest.transcript_watermarks,
                      engram_ingest.issue_cursors, engram_ingest.sources
             RESTART IDENTITY CASCADE",
        )
        .expect("truncate");
}

fn setup(client: &mut Client) -> (PathBuf, TranscriptSource) {
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .expect("database name")
        .get(0);
    assert_eq!(
        database, "engram_ingest_test",
        "refusing to truncate a database that is not the dedicated engram_ingest_test database"
    );
    client
        .execute("SELECT pg_advisory_lock($1)", &[&918_273_i64])
        .expect("harness lock");
    migrate(client).expect("migrate");
    truncate(client);
    let root = std::env::temp_dir().join(format!(
        "engram-transcript-db-{}-{}",
        std::process::id(),
        Utc::now().timestamp_nanos_opt().unwrap_or_default()
    ));
    fs::create_dir_all(root.join("workspace-demo").join("agent-transcripts")).unwrap();
    let source = TranscriptSource {
        project: "engram-test".to_string(),
        tag: "transcripts".to_string(),
        root: root.clone(),
        bank_id: "engram-test-transcripts".to_string(),
        workspace_prefixes: vec!["workspace-".to_string()],
    };
    (root, source)
}

fn source_id(client: &mut Client) -> i64 {
    client
        .query_one(
            "SELECT id FROM engram_ingest.sources WHERE source_kind = 'transcript'",
            &[],
        )
        .expect("source")
        .get(0)
}

fn transcript_path(root: &Path, name: &str) -> PathBuf {
    root.join("workspace-demo")
        .join("agent-transcripts")
        .join(name)
}

fn relative_transcript(name: &str) -> String {
    format!("workspace-demo/agent-transcripts/{name}.jsonl")
}

fn write_messages(path: &Path, messages: &[serde_json::Value]) {
    let body = messages
        .iter()
        .map(|message| serde_json::to_string(message).unwrap())
        .collect::<Vec<_>>()
        .join("\n");
    fs::write(path, body + "\n").unwrap();
}

fn user(text: &str) -> serde_json::Value {
    json!({"role": "user", "message": {"content": text}})
}

fn assistant(text: &str) -> serde_json::Value {
    json!({"role": "assistant", "message": {"content": text}})
}

fn ack_all(client: &mut Client) {
    while let Some(job) = claim_job(
        client,
        "engram-test-transcripts",
        "test-worker",
        Duration::seconds(3_600),
        Utc::now(),
    )
    .expect("claim")
    {
        complete_job(client, job.job_id, &job.token, Utc::now()).expect("complete");
    }
}

fn job_count(client: &mut Client, state: &str) -> i64 {
    client
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs WHERE state = $1",
            &[&state],
        )
        .unwrap()
        .get(0)
}

fn active_chunks(client: &mut Client) -> i64 {
    client
        .query_one(
            "SELECT count(*) FROM engram_ingest.chunks WHERE state = 'active'",
            &[],
        )
        .unwrap()
        .get(0)
}

#[test]
fn watermark_survives_restart_replay_and_failed_retains() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let (root, source) = setup(&mut client);
    let path = transcript_path(&root, "session.jsonl");
    let base = vec![
        user("first message"),
        assistant("assistant one"),
        user("Always use tests before proceeding."),
        assistant("assistant two"),
        user("plain tail message"),
    ];
    write_messages(&path, &base);
    let options = TranscriptOptions::default();

    let first =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("first");
    assert_eq!(first.files[0].start_index, 0);
    assert_eq!(first.files[0].windows_retained, 1);
    assert_eq!(first.summary.jobs_created, 1);
    let first_payload: serde_json::Value = client
        .query_one(
            "SELECT payload FROM engram_ingest.jobs WHERE kind = 'retain' ORDER BY id LIMIT 1",
            &[],
        )
        .unwrap()
        .get(0);
    assert_eq!(first_payload["metadata"]["source"], "cocoindex-transcript");
    assert_eq!(first_payload["metadata"]["transcript_id"], "session");
    assert_eq!(first_payload["tags"], json!(["engram-test"]));
    let id = source_id(&mut client);
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("session"))
            .expect("watermark read")
            .expect("a pending scan must still save a watermark row")
            .message_count,
        0,
        "a pending retain must not advance the watermark"
    );

    // Crash before ack: replay is a no-op and still one durable job.
    let replay =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("replay");
    assert_eq!(replay.summary.jobs_created, 0);
    assert_eq!(job_count(&mut client, "pending"), 1);
    assert_eq!(active_chunks(&mut client), 1);

    // Worker ack advances the watermark on the next scan refresh.
    ack_all(&mut client);
    let refreshed =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("refreshed");
    assert_eq!(refreshed.summary.jobs_created, 0);
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("session"))
            .unwrap()
            .unwrap()
            .message_count,
        5
    );

    // Crash after ack: replay neither re-enqueues nor regresses.
    let after_ack =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("after ack");
    assert_eq!(after_ack.summary.jobs_created, 0);
    assert_eq!(job_count(&mut client, "succeeded"), 1);
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("session"))
            .unwrap()
            .unwrap()
            .message_count,
        5
    );

    // Append a new signal: only the new window is retained.
    let mut appended = base.clone();
    appended.push(user("we never deploy on Friday"));
    appended.push(assistant("understood"));
    write_messages(&path, &appended);
    let append =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("append");
    assert_eq!(append.files[0].start_index, 5);
    assert_eq!(append.files[0].windows_retained, 1);
    assert_eq!(append.summary.jobs_created, 1);
    ack_all(&mut client);
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("append refresh");
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("session"))
            .unwrap()
            .unwrap()
            .message_count,
        7
    );

    // Failed retain: dead job keeps the watermark at 7 and cannot duplicate.
    let mut failing = appended.clone();
    failing.push(user("No, that's wrong; use the other approach."));
    failing.push(user("plain tail two"));
    write_messages(&path, &failing);
    let failed_plan =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("failed plan");
    assert_eq!(failed_plan.summary.jobs_created, 1);
    let base = Utc::now();
    for attempt in 0..3 {
        let now = base + Duration::seconds(10 * (attempt + 1));
        let job = claim_job(
            &mut client,
            "engram-test-transcripts",
            "test-worker",
            Duration::seconds(3_600),
            now,
        )
        .unwrap()
        .unwrap();
        fail_job(
            &mut client,
            job.job_id,
            &job.token,
            now,
            "permanent",
            "sink 400",
        )
        .unwrap_or_else(|error| panic!("fail job: {error}"));
    }
    assert_eq!(job_count(&mut client, "dead"), 1);
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("session"))
            .unwrap()
            .unwrap()
            .message_count,
        7,
        "a dead retain must leave the watermark unadvanced"
    );
    let replay_failed = ingest_transcript_source(&mut client, &source, &options, Utc::now())
        .expect("failed replay");
    assert_eq!(replay_failed.summary.jobs_created, 0);
    assert_eq!(job_count(&mut client, "dead"), 1);

    // Malformed/partial concurrent write is ignored until the line is valid.
    let mut partial = fs::read_to_string(&path).unwrap();
    partial.push_str("{\"role\":\"user\",\"message\":{\"content\":\"we never");
    fs::write(&path, partial).unwrap();
    let partial_scan =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("partial");
    assert_eq!(partial_scan.files[0].message_count, 9);
    assert_eq!(partial_scan.summary.jobs_created, 0);

    // Truncation resets the start index and tombstones windows that no longer
    // exist in the rewritten file.
    let truncated = vec![
        user("first message"),
        assistant("assistant one"),
        user("Always use tests before proceeding."),
    ];
    write_messages(&path, &truncated);
    let truncated_scan =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("truncated");
    assert_eq!(truncated_scan.files[0].start_index, 0);
    assert!(truncated_scan.summary.deleted_chunks >= 1);
    assert!(active_chunks(&mut client) >= 1);
    ack_all(&mut client);
    ingest_transcript_source(&mut client, &source, &options, Utc::now())
        .expect("truncated refresh");
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("session"))
            .unwrap()
            .unwrap()
            .message_count,
        3,
        "a rewrite may lower the watermark to the new scanned count"
    );

    // Removal tombstones the file's remaining windows and drops its watermark.
    fs::remove_file(&path).unwrap();
    let removed =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("removed");
    assert_eq!(
        removed.removed,
        vec!["workspace-demo/agent-transcripts/session.jsonl"]
    );
    assert!(
        removed.summary.deleted_chunks >= 1,
        "removed transcript windows must be deleted"
    );
    assert!(
        client
            .query_one(
                "SELECT count(*) FROM engram_ingest.jobs WHERE kind = 'delete'",
                &[]
            )
            .unwrap()
            .get::<_, i64>(0)
            >= 1
    );
    assert!(
        client
            .query_one(
                "SELECT count(*) FROM engram_ingest.tombstones WHERE reason = 'transcript_removed'",
                &[]
            )
            .unwrap()
            .get::<_, i64>(0)
            >= 1
    );
    assert!(
        load_transcript_watermark(&mut client, id, &relative_transcript("session"))
            .unwrap()
            .is_none()
    );

    // A signal-free transcript has no document row; its watermark must still be
    // cleaned up when the file disappears.
    let quiet = transcript_path(&root, "quiet.jsonl");
    write_messages(&quiet, &[user("plain one"), assistant("plain two")]);
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("quiet");
    assert!(
        load_transcript_watermark(&mut client, id, &relative_transcript("quiet"))
            .unwrap()
            .is_some()
    );
    fs::remove_file(&quiet).unwrap();
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("quiet removed");
    assert!(
        load_transcript_watermark(&mut client, id, &relative_transcript("quiet"))
            .unwrap()
            .is_none()
    );

    fs::remove_dir_all(&root).ok();
}

#[derive(Default)]
struct SpyInvalidator {
    calls: Arc<Mutex<Vec<(String, String, String)>>>,
}

impl MemoryInvalidator for SpyInvalidator {
    fn invalidate(&self, bank_id: &str, memory_id: &str, reason: &str) -> bool {
        self.calls.lock().unwrap().push((
            bank_id.to_string(),
            memory_id.to_string(),
            reason.to_string(),
        ));
        true
    }
}

struct FlakyInvalidator {
    calls: Arc<Mutex<usize>>,
    fail_first: usize,
}

impl MemoryInvalidator for FlakyInvalidator {
    fn invalidate(&self, _bank_id: &str, _memory_id: &str, _reason: &str) -> bool {
        let mut calls = self.calls.lock().unwrap();
        *calls += 1;
        *calls > self.fail_first
    }
}

struct FixedResolver {
    action: ResolutionAction,
}

impl ContradictionResolver for FixedResolver {
    fn resolve(
        &self,
        _bank_id: &str,
        _statement: &str,
        _project: Option<&str>,
    ) -> ContradictionResolution {
        ContradictionResolution {
            action: self.action,
            superseded_memory_id: Some("mem-1".to_string()),
            superseded_document_id: Some("doc-1".to_string()),
            confidence: 0.95,
            explanation: "fixture".to_string(),
        }
    }
}

fn correction_messages() -> Vec<serde_json::Value> {
    vec![
        user("context before"),
        assistant("ack"),
        user("No, that's wrong; use the other approach."),
        assistant("fixed"),
        user("context after"),
    ]
}

fn tags_for_first_job(client: &mut Client) -> Vec<String> {
    let payload: serde_json::Value = client
        .query_one(
            "SELECT payload FROM engram_ingest.jobs WHERE kind = 'retain' ORDER BY id LIMIT 1",
            &[],
        )
        .unwrap()
        .get(0);
    payload["tags"]
        .as_array()
        .unwrap()
        .iter()
        .map(|value| value.as_str().unwrap().to_string())
        .collect()
}

#[test]
fn equal_or_larger_rewrite_and_in_place_rotation_restart_from_zero() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let (root, source) = setup(&mut client);
    let options = TranscriptOptions::default();
    let path = transcript_path(&root, "rewrite.jsonl");
    let first = vec![
        user("Always use tests before proceeding."),
        assistant("ack"),
        user("tail"),
    ];
    write_messages(&path, &first);
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("first");
    ack_all(&mut client);
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("refresh");
    let id = source_id(&mut client);
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("rewrite"))
            .unwrap()
            .unwrap()
            .message_count,
        3
    );
    let old_chunks = active_chunks(&mut client);

    // Same length but a different prefix: the count watermark alone would skip
    // the new correction at index 0.
    let rewritten = vec![
        user("No, that's wrong; use the other approach."),
        assistant("ack"),
        user("tail"),
        user("Always use tests too."),
    ];
    write_messages(&path, &rewritten);
    let rewrite =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("rewrite");
    assert_eq!(rewrite.files[0].start_index, 0);
    assert_eq!(rewrite.files[0].windows_retained, 2);
    assert!(
        rewrite.summary.deleted_chunks >= 1,
        "rewritten-away windows must tombstone"
    );
    assert!(active_chunks(&mut client) >= old_chunks);
    ack_all(&mut client);
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("rewrite refresh");
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("rewrite"))
            .unwrap()
            .unwrap()
            .message_count,
        4
    );
    let before_rotation = active_chunks(&mut client);

    // Rotation in place: old content moves to a sibling name; the configured
    // path keeps its identity but the prefix changes.
    fs::rename(&path, transcript_path(&root, "rewrite.jsonl.1")).unwrap();
    write_messages(
        &path,
        &[
            user("No, that's wrong; use another approach."),
            assistant("ack"),
            user("new tail"),
        ],
    );
    let rotation =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("rotation");
    assert_eq!(rotation.files[0].start_index, 0);
    assert!(rotation.summary.deleted_chunks >= 1);
    assert!(active_chunks(&mut client) <= before_rotation + 1);
    fs::remove_dir_all(&root).ok();
}

#[test]
fn live_invalidation_retries_until_acknowledged_without_duplicate_patch() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let (root, source) = setup(&mut client);
    let calls = Arc::new(Mutex::new(0));
    let options = TranscriptOptions::default()
        .with_mode(ContradictionMode::Live)
        .with_resolver(Box::new(FixedResolver {
            action: ResolutionAction::AutoResolved,
        }))
        .with_invalidator(Box::new(FlakyInvalidator {
            calls: Arc::clone(&calls),
            fail_first: 2,
        }));
    let path = transcript_path(&root, "flaky.jsonl");
    write_messages(&path, &correction_messages());
    let first =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("first");
    assert_eq!(first.invalidated, 0);
    assert_eq!(*calls.lock().unwrap(), 1);
    let pending: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.transcript_windows
              WHERE invalidation_state = 'pending'",
            &[],
        )
        .unwrap()
        .get(0);
    assert_eq!(pending, 1);
    // A rewrite that removes the window must not drop the committed intent.
    write_messages(&path, &[user("plain one"), assistant("plain two")]);
    let rewritten = ingest_transcript_source(&mut client, &source, &options, Utc::now())
        .expect("pending rewrite");
    assert_eq!(rewritten.invalidated, 0);
    let still_pending: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.transcript_windows
              WHERE invalidation_state = 'pending'",
            &[],
        )
        .unwrap()
        .get(0);
    assert_eq!(still_pending, 1);
    let second =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("retry");
    assert_eq!(second.invalidated, 1);
    assert_eq!(*calls.lock().unwrap(), 3);
    let remaining: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.transcript_windows
              WHERE invalidation_state = 'pending'",
            &[],
        )
        .unwrap()
        .get(0);
    assert_eq!(remaining, 0);
    let third =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("acknowledged");
    assert_eq!(third.invalidated, 0);
    assert_eq!(*calls.lock().unwrap(), 3);
    fs::remove_dir_all(&root).ok();
}

#[test]
fn rewrite_is_detected_while_the_first_retain_is_still_pending() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let (root, source) = setup(&mut client);
    let options = TranscriptOptions::default();
    let path = transcript_path(&root, "pending-rewrite.jsonl");
    write_messages(&path, &correction_messages());
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("first");
    assert_eq!(job_count(&mut client, "pending"), 1);
    write_messages(
        &path,
        &[
            user("No, that's wrong; use a different approach."),
            assistant("ack"),
            user("new tail"),
            user("Always use tests too."),
        ],
    );
    let second = ingest_transcript_source(&mut client, &source, &options, Utc::now())
        .expect("pending rewrite");
    assert_eq!(second.files[0].start_index, 0);
    assert!(
        second.summary.deleted_chunks >= 1,
        "the pre-ack replacement must tombstone obsolete windows"
    );
    fs::remove_dir_all(&root).ok();
}

#[test]
fn append_after_tail_signal_does_not_duplicate_the_occurrence() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let (root, source) = setup(&mut client);
    let options = TranscriptOptions::default();
    let path = transcript_path(&root, "tail.jsonl");
    write_messages(&path, &[user("Always use tests before proceeding.")]);
    let first =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("tail signal");
    assert_eq!(first.summary.jobs_created, 1);
    write_messages(
        &path,
        &[
            user("Always use tests before proceeding."),
            assistant("reply"),
        ],
    );
    let replay =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("tail append");
    assert_eq!(
        replay.summary.jobs_created, 0,
        "the rendered window changed, but the same signal must not be retained twice"
    );
    assert_eq!(job_count(&mut client, "pending"), 1);
    fs::remove_dir_all(&root).ok();
}

#[test]
fn redrive_guard_skips_retains_whose_chunk_was_deleted() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let (root, source) = setup(&mut client);
    let options = TranscriptOptions::default();
    let path = transcript_path(&root, "dead.jsonl");
    write_messages(&path, &correction_messages());
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("ingest");
    let base = Utc::now();
    for attempt in 0..3 {
        let now = base + Duration::seconds(10 * (attempt + 1));
        let job = claim_job(
            &mut client,
            "engram-test-transcripts",
            "test-worker",
            Duration::seconds(3_600),
            now,
        )
        .unwrap()
        .unwrap();
        fail_job(
            &mut client,
            job.job_id,
            &job.token,
            now,
            "permanent",
            "sink 400",
        )
        .unwrap();
    }
    fs::remove_file(&path).unwrap();
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("remove");
    let base = Utc::now() + Duration::seconds(60);
    for attempt in 0..3 {
        let now = base + Duration::seconds(10 * (attempt + 1));
        let job = claim_job(
            &mut client,
            "engram-test-transcripts",
            "test-worker",
            Duration::seconds(3_600),
            now,
        )
        .unwrap()
        .unwrap();
        fail_job(
            &mut client,
            job.job_id,
            &job.token,
            now,
            "permanent",
            "sink 400",
        )
        .unwrap();
    }
    // Restore the same window: its chunk generation advances and the old dead
    // retain/delete must not be requeued over the restored memory.
    write_messages(&path, &correction_messages());
    ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("restore");
    client
        .batch_execute(
            "UPDATE engram_ingest.jobs j
                SET state = 'pending', attempts = 0, run_after = now(), updated_at = now()
              WHERE j.state = 'dead'
                AND j.source_id IN (
                    SELECT id FROM engram_ingest.sources WHERE source_kind = 'transcript'
                )
                AND (
                    j.kind <> 'retain'
                    OR EXISTS (
                        SELECT 1 FROM engram_ingest.chunks c
                         WHERE c.source_id = j.source_id
                           AND c.hindsight_document_id = j.hindsight_document_id
                           AND c.state = 'active'
                           AND c.generation = j.generation
                    )
                )
                AND (
                    j.kind <> 'delete'
                    OR EXISTS (
                        SELECT 1 FROM engram_ingest.chunks c
                         WHERE c.source_id = j.source_id
                           AND c.hindsight_document_id = j.hindsight_document_id
                           AND c.state = 'tombstoned'
                           AND c.generation = j.generation
                    )
                )",
        )
        .expect("guarded redrive");
    let dead: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs WHERE state = 'dead'",
            &[],
        )
        .unwrap()
        .get(0);
    assert!(
        dead >= 2,
        "both the old retain and delete must stay dead after restore"
    );
    fs::remove_dir_all(&root).ok();
}

#[test]
fn same_stem_files_are_skipped_not_a_source_failure() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let (root, source) = setup(&mut client);
    let options = TranscriptOptions::default();
    let messages = correction_messages();
    let mut extended = messages.clone();
    extended.push(user("Always use tests too."));
    for (workspace, content) in [("workspace-a", &messages), ("workspace-b", &extended)] {
        let directory = root
            .join(workspace)
            .join("agent-transcripts")
            .join("session");
        fs::create_dir_all(&directory).unwrap();
        write_messages(&directory.join("session.jsonl"), content);
    }
    let outcome =
        ingest_transcript_source(&mut client, &source, &options, Utc::now()).expect("source");
    assert_eq!(
        outcome
            .files
            .iter()
            .map(|file| file.windows_conflicted)
            .sum::<usize>(),
        1
    );
    assert_eq!(
        outcome
            .files
            .iter()
            .map(|file| file.windows_retained)
            .sum::<usize>(),
        2,
        "the later file's distinct instruction is preserved"
    );
    fs::remove_dir_all(&root).ok();
}

#[test]
fn contradiction_modes_gate_queue_tags_and_live_invalidation() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let (root, source) = setup(&mut client);
    let path = transcript_path(&root, "queued.jsonl");
    write_messages(&path, &correction_messages());
    let queued = TranscriptOptions::default().with_resolver(Box::new(FixedResolver {
        action: ResolutionAction::Queued,
    }));
    let outcome =
        ingest_transcript_source(&mut client, &source, &queued, Utc::now()).expect("queued");
    let id = source_id(&mut client);
    assert_eq!(outcome.windows_queued, 1);
    assert_eq!(outcome.summary.jobs_created, 0);
    assert_eq!(job_count(&mut client, "pending"), 0);
    let queued_status: String = client
        .query_one(
            "SELECT status FROM engram_ingest.transcript_windows
              WHERE transcript_id = 'queued' LIMIT 1",
            &[],
        )
        .unwrap()
        .get(0);
    assert_eq!(queued_status, "queued");
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("queued"))
            .unwrap()
            .unwrap()
            .message_count,
        5,
        "a withheld correction advances the watermark because no job is pending"
    );
    // A queued ownership row must not survive a rewrite that removes the window.
    write_messages(&path, &[user("plain one"), assistant("plain two")]);
    ingest_transcript_source(&mut client, &source, &queued, Utc::now()).expect("queued rewrite");
    let remaining: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.transcript_windows
              WHERE relative_path = $1",
            &[&relative_transcript("queued")],
        )
        .unwrap()
        .get(0);
    assert_eq!(remaining, 0);
    assert_eq!(
        load_transcript_watermark(&mut client, id, &relative_transcript("queued"))
            .unwrap()
            .unwrap()
            .message_count,
        2,
        "the rewrite lowers the watermark to the new scanned count"
    );

    let shadow_invalidator = Arc::new(Mutex::new(Vec::new()));
    let shadow_ops = TranscriptOptions::default()
        .with_mode(ContradictionMode::Shadow)
        .with_resolver(Box::new(FixedResolver {
            action: ResolutionAction::AutoResolved,
        }))
        .with_invalidator(Box::new(SpyInvalidator {
            calls: Arc::clone(&shadow_invalidator),
        }));
    let shadow_path = transcript_path(&root, "shadow.jsonl");
    write_messages(&shadow_path, &correction_messages());
    let shadow =
        ingest_transcript_source(&mut client, &source, &shadow_ops, Utc::now()).expect("shadow");
    assert_eq!(shadow.windows_auto_resolved, 1);
    assert_eq!(shadow.invalidated, 0);
    assert!(shadow_invalidator.lock().unwrap().is_empty());
    let tags = tags_for_first_job(&mut client);
    assert!(tags.contains(&"CORRECTION".to_string()));
    assert!(tags.contains(&"supersedes-prior-memory".to_string()));

    let live_calls = Arc::new(Mutex::new(Vec::new()));
    let live_ops = TranscriptOptions::default()
        .with_mode(ContradictionMode::Live)
        .with_resolver(Box::new(FixedResolver {
            action: ResolutionAction::AutoResolved,
        }))
        .with_invalidator(Box::new(SpyInvalidator {
            calls: Arc::clone(&live_calls),
        }));
    let live_path = transcript_path(&root, "live.jsonl");
    write_messages(&live_path, &correction_messages());
    let live = ingest_transcript_source(&mut client, &source, &live_ops, Utc::now()).expect("live");
    assert_eq!(live.invalidated, 1);
    let calls = live_calls.lock().unwrap();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].0, "engram-test-transcripts");
    assert_eq!(calls[0].1, "mem-1");
    assert!(calls[0].2.starts_with("superseded: "));
    drop(calls);
    let replay =
        ingest_transcript_source(&mut client, &source, &live_ops, Utc::now()).expect("live replay");
    assert_eq!(replay.invalidated, 0);
    assert_eq!(live_calls.lock().unwrap().len(), 1);

    // A correction that resolves to retain keeps only the project tag.
    let retain_path = transcript_path(&root, "retain.jsonl");
    write_messages(&retain_path, &correction_messages());
    let retain = TranscriptOptions::default().with_resolver(Box::new(FixedResolver {
        action: ResolutionAction::Retain,
    }));
    let _ = ingest_transcript_source(&mut client, &source, &retain, Utc::now()).expect("retain");
    let tagged: serde_json::Value = client
        .query_one(
            "SELECT payload FROM engram_ingest.jobs
              WHERE payload->>'document_id' LIKE 'transcript-retain-%' LIMIT 1",
            &[],
        )
        .unwrap()
        .get(0);
    assert_eq!(tagged["tags"], json!(["engram-test"]));
    fs::remove_dir_all(&root).ok();
}
