//! End-to-end ledger + Hindsight sink harness.

mod support;

use chrono::{Duration as LedgerDuration, Utc};
use engram_ingest::{
    apply_reconciliation, claim_job, connect, load_manifest, migrate, plan_document, process_one,
    reconcile, upsert_source, ContentHash, DeleteOutcome, HindsightSink, ScannedFile, SinkError,
    SinkOutcome, SourceRecord, StoreError, LAYOUT_VERSION,
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

    // v1 -> v2 upgrade path: drop the v2 column and version, then re-migrate.
    client
        .batch_execute(
            "ALTER TABLE engram_ingest.jobs DROP COLUMN last_error_class;
             DELETE FROM engram_ingest.schema_migrations WHERE version = 2",
        )
        .expect("downgrade to v1");
    migrate(&mut client).expect("v2 upgrade");
    let v2_applied: bool = client
        .query_one(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_schema='engram_ingest' AND table_name='jobs'
                AND column_name='last_error_class')",
            &[],
        )
        .expect("column")
        .get(0);
    assert!(v2_applied);

    // A database ahead of the binary is rejected, not silently accepted.
    client
        .execute(
            "INSERT INTO engram_ingest.schema_migrations (version) VALUES (99)",
            &[],
        )
        .expect("unknown version");
    assert!(matches!(
        migrate(&mut client),
        Err(StoreError::MigrationMismatch(_))
    ));
    client
        .execute(
            "DELETE FROM engram_ingest.schema_migrations WHERE version = 99",
            &[],
        )
        .expect("cleanup version");
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
    let replay =
        apply_reconciliation(&mut client, source, &plan, Utc::now()).expect("replay apply");
    assert_eq!(
        replay.jobs_created, 0,
        "replaying the plan must not enqueue"
    );

    let (url, log) = support::one_shot(200, r#"{"ok":true}"#);
    let sink = HindsightSink::new(url).expect("sink");
    assert!(matches!(
        process_one(
            &mut client,
            &sink,
            "repo-docs",
            "worker-sink",
            LedgerDuration::seconds(1),
            Utc::now(),
        ),
        Err(SinkError::InvalidLease(_))
    ));
    let processed = process_one(
        &mut client,
        &sink,
        "repo-docs",
        "worker-sink",
        LedgerDuration::seconds(180),
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
        LedgerDuration::seconds(180),
        Utc::now(),
    )
    .expect("process")
    .expect("one job");
    assert!(matches!(failed.outcome, SinkOutcome::Failed(_)));
    let (state, attempts) = state_of(&mut client, failed.job_id);
    assert_eq!(state, "pending");
    assert_eq!(attempts, 1);
    let (class, last_error): (Option<String>, Option<String>) = {
        let row = client
            .query_one(
                "SELECT last_error_class, last_error FROM engram_ingest.jobs WHERE id = $1",
                &[&failed.job_id],
            )
            .expect("error class");
        (row.get(0), row.get(1))
    };
    assert_eq!(class.as_deref(), Some("transient"));
    assert!(
        !last_error.unwrap_or_default().contains("Alpha body"),
        "job errors must not persist document content"
    );
    let failed_document_id: String = client
        .query_one(
            "SELECT hindsight_document_id FROM engram_ingest.jobs WHERE id = $1",
            &[&failed.job_id],
        )
        .expect("job id")
        .get(0);
    assert_eq!(failed_document_id, "repo--sink");

    // A permanent 4xx is recorded as such and still consumes the retry budget.
    client
        .execute(
            "UPDATE engram_ingest.jobs SET state = 'succeeded' WHERE state = 'pending'",
            &[],
        )
        .expect("drain");
    let edited_again = "# A\n\nAlpha body edited twice and long enough.";
    let edited_again_scan = vec![scanned("sink.md", edited_again)];
    let manifest = load_manifest(&mut client, source).expect("manifest");
    let plan = reconcile(&manifest, &edited_again_scan, LAYOUT_VERSION);
    apply_reconciliation(&mut client, source, &plan, Utc::now()).expect("edit again");
    let (url, _) = support::one_shot(400, "{}");
    let sink = HindsightSink::new(url).expect("sink");
    let permanent = process_one(
        &mut client,
        &sink,
        "repo-docs",
        "worker-sink",
        LedgerDuration::seconds(180),
        Utc::now(),
    )
    .expect("process")
    .expect("one job");
    let (state, attempts) = state_of(&mut client, permanent.job_id);
    assert_eq!(state, "pending");
    assert_eq!(attempts, 1);
    let class: Option<String> = client
        .query_one(
            "SELECT last_error_class FROM engram_ingest.jobs WHERE id = $1",
            &[&permanent.job_id],
        )
        .expect("class")
        .get(0);
    assert_eq!(class.as_deref(), Some("permanent"));

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
    let (url, log) = support::one_shot(404, "{}");
    let sink = HindsightSink::new(url).expect("sink");
    let deleted = process_one(
        &mut client,
        &sink,
        "repo-docs",
        "worker-sink",
        LedgerDuration::seconds(180),
        Utc::now(),
    )
    .expect("process")
    .expect("one job");
    assert_eq!(
        deleted.outcome,
        SinkOutcome::Deleted(DeleteOutcome::AlreadyAbsent)
    );
    assert_eq!(state_of(&mut client, deleted.job_id).0, "succeeded");
    let delete_request = log.lock().unwrap()[0].clone();
    assert!(
        delete_request.starts_with("DELETE /v1/default/banks/repo-docs/documents/repo--sink "),
        "{delete_request}"
    );
    let acked: Option<chrono::DateTime<Utc>> = client
        .query_one(
            "SELECT sink_deleted_at FROM engram_ingest.tombstones
              WHERE source_id = $1 AND sink_deleted_at IS NOT NULL",
            &[&source],
        )
        .expect("tombstone")
        .get(0);
    assert!(acked.is_some());

    // A structurally invalid payload is classified permanent and exhausts the
    // ledger retry budget into `dead` rather than staying leased forever.
    truncate(&mut client);
    let source = upsert_source(
        &mut client,
        &SourceRecord::markdown("engram-test", "bad", "/tmp/bad", &["**/*.md"]),
    )
    .expect("source");
    let job_id: i64 = client
        .query_one(
            "INSERT INTO engram_ingest.jobs
                 (source_id, kind, hindsight_document_id, dedupe_key, generation,
                  payload, state, run_after)
             VALUES ($1, 'retain', 'bad--doc', 'bad', 1, '{}'::jsonb, 'pending', now())
             RETURNING id",
            &[&source],
        )
        .expect("bad job")
        .get(0);
    let (url, _) = support::one_shot(200, "{}");
    let sink = HindsightSink::new(url).expect("sink");
    let base = Utc::now();
    for attempt in 0..3 {
        process_one(
            &mut client,
            &sink,
            "repo-docs",
            "worker-sink",
            LedgerDuration::seconds(180),
            base + LedgerDuration::seconds(attempt * 10),
        )
        .expect("process");
    }
    let (state, attempts, class): (String, i32, Option<String>) = {
        let row = client
            .query_one(
                "SELECT state, attempts, last_error_class
                   FROM engram_ingest.jobs WHERE id = $1",
                &[&job_id],
            )
            .expect("job");
        (row.get(0), row.get(1), row.get(2))
    };
    assert_eq!(state, "dead");
    assert_eq!(attempts, 3);
    assert_eq!(class.as_deref(), Some("permanent"));

    // A crashed worker's expired lease is reclaimed before the next claim, so
    // it does not block the document queue while a worker is running.
    truncate(&mut client);
    let source = upsert_source(
        &mut client,
        &SourceRecord::markdown("engram-test", "lease", "/tmp/lease", &["**/*.md"]),
    )
    .expect("source");
    let scan = vec![scanned("lease.md", "# A\n\nLease body long enough.")];
    let manifest = load_manifest(&mut client, source).expect("manifest");
    let plan = reconcile(&manifest, &scan, LAYOUT_VERSION);
    apply_reconciliation(&mut client, source, &plan, Utc::now()).expect("apply");
    let t0 = Utc::now();
    let abandoned = claim_job(
        &mut client,
        "crashed-worker",
        LedgerDuration::seconds(1),
        t0,
    )
    .expect("claim")
    .expect("job");
    let (url, _) = support::one_shot(200, "{}");
    let sink = HindsightSink::new(url).expect("sink");
    // Recovery requeues with its 1s backoff, so the first pass reclaims only.
    assert!(process_one(
        &mut client,
        &sink,
        "repo-docs",
        "live-worker",
        LedgerDuration::seconds(180),
        t0 + LedgerDuration::seconds(2),
    )
    .expect("recover")
    .is_none());
    let recovered = process_one(
        &mut client,
        &sink,
        "repo-docs",
        "live-worker",
        LedgerDuration::seconds(180),
        t0 + LedgerDuration::seconds(5),
    )
    .expect("process")
    .expect("reclaimed job");
    assert_eq!(recovered.job_id, abandoned.job_id);
    assert_eq!(recovered.outcome, SinkOutcome::Retained);
    let (state, attempts) = state_of(&mut client, recovered.job_id);
    assert_eq!(state, "succeeded");
    assert_eq!(attempts, 1);

    truncate(&mut client);
}
