//! End-to-end ledger + Hindsight sink harness.

mod support;

use chrono::{Duration as LedgerDuration, Utc};
use engram_ingest::{
    apply_reconciliation, connect, load_manifest, migrate, plan_document, process_one, reconcile,
    upsert_source, ContentHash, DeleteOutcome, HindsightSink, ScannedFile, SinkOutcome,
    SourceRecord, LAYOUT_VERSION,
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

fn state_of(client: &mut Client, job_id: i64) -> (String, i32) {
    let row = client
        .query_one(
            "SELECT state, attempts FROM engram_ingest.jobs WHERE id = $1",
            &[&job_id],
        )
        .expect("job row");
    (row.get(0), row.get(1))
}

#[test]
fn sink_retains_deletes_and_schedules_retries() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping sink harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .expect("database")
        .get(0);
    assert_eq!(database, "engram_ingest_test");
    client
        .execute("SELECT pg_advisory_lock($1)", &[&918_273_i64])
        .expect("harness lock");
    migrate(&mut client).expect("migrate");
    truncate(&mut client);

    let source = upsert_source(
        &mut client,
        &SourceRecord::markdown("engram-test", "sink", "/tmp/sink", &["**/*.md"]),
    )
    .expect("source");
    let original = "# A\n\nAlpha body long enough.";
    let scan = vec![scanned("sink.md", original)];
    let manifest = load_manifest(&mut client, source).expect("manifest");
    let plan = reconcile(&manifest, &scan, LAYOUT_VERSION);
    apply_reconciliation(&mut client, source, &plan, Utc::now()).expect("apply");

    let (url, log) = support::one_shot(200, r#"{"ok":true}"#);
    let sink = HindsightSink::new(url).expect("sink");
    let processed = process_one(
        &mut client,
        &sink,
        "repo-docs",
        "worker-sink",
        LedgerDuration::seconds(60),
        Utc::now(),
    )
    .expect("process")
    .expect("one job");
    assert_eq!(processed.outcome, SinkOutcome::Retained);
    assert!(state_of(&mut client, processed.job_id).0 == "succeeded");
    assert!(log.lock().unwrap()[0].starts_with("POST /v1/default/banks/repo-docs/memories"));

    // Failure is recorded on the ledger, not swallowed.
    let edited = "# A\n\nAlpha body edited and long enough.";
    let edited_scan = vec![scanned("sink.md", edited)];
    let manifest = load_manifest(&mut client, source).expect("manifest");
    let plan = reconcile(&manifest, &edited_scan, LAYOUT_VERSION);
    apply_reconciliation(&mut client, source, &plan, Utc::now()).expect("edit");
    let (url, _) = support::one_shot(503, "{}");
    let sink = HindsightSink::new(url).expect("sink");
    let failed = process_one(
        &mut client,
        &sink,
        "repo-docs",
        "worker-sink",
        LedgerDuration::seconds(60),
        Utc::now(),
    )
    .expect("process")
    .expect("one job");
    assert!(matches!(failed.outcome, SinkOutcome::Failed(_)));
    let (state, attempts) = state_of(&mut client, failed.job_id);
    assert_eq!(state, "pending");
    assert_eq!(attempts, 1);

    // Drain the retry so the delete can be claimed, then 404 means success.
    client
        .execute(
            "UPDATE engram_ingest.jobs SET state = 'succeeded' WHERE state = 'pending'",
            &[],
        )
        .expect("drain");
    let manifest = load_manifest(&mut client, source).expect("manifest");
    let plan = reconcile(&manifest, &[], LAYOUT_VERSION);
    apply_reconciliation(&mut client, source, &plan, Utc::now()).expect("delete plan");
    let (url, _) = support::one_shot(404, "{}");
    let sink = HindsightSink::new(url).expect("sink");
    let deleted = process_one(
        &mut client,
        &sink,
        "repo-docs",
        "worker-sink",
        LedgerDuration::seconds(60),
        Utc::now(),
    )
    .expect("process")
    .expect("one job");
    assert_eq!(
        deleted.outcome,
        SinkOutcome::Deleted(DeleteOutcome::AlreadyAbsent)
    );
    assert_eq!(state_of(&mut client, deleted.job_id).0, "succeeded");
    let acked: Option<chrono::DateTime<Utc>> = client
        .query_one(
            "SELECT sink_deleted_at FROM engram_ingest.tombstones
              WHERE source_id = $1 AND sink_deleted_at IS NOT NULL",
            &[&source],
        )
        .expect("tombstone")
        .get(0);
    assert!(acked.is_some());

    truncate(&mut client);
}
