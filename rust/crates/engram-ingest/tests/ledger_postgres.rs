//! One-root Postgres convergence harness for the ingestion ledger.
//!
//! Requires `ENGRAM_INGEST_TEST_DATABASE_URL` pointing at a **disposable**
//! database whose name contains `test` (CI provides one). The test truncates the
//! `engram_ingest` tables by design; it refuses to run against any other name.

use chrono::{Duration, Utc};
use engram_ingest::{
    apply_reconciliation, claim_job, complete_job, connect, fail_job, heartbeat_job, load_manifest,
    migrate, plan_document, reconcile, recover_expired_leases, recover_expired_leases_for_banks,
    recover_expired_leases_in_bank, upsert_source, ContentHash, ScannedFile, SourceRecord,
    StoreError, LAYOUT_VERSION,
};
use postgres::Client;

fn test_url() -> Option<String> {
    std::env::var("ENGRAM_INGEST_TEST_DATABASE_URL")
        .ok()
        .filter(|value| !value.trim().is_empty())
}

fn scanned(path: &str, content: &str) -> ScannedFile {
    let calls = plan_document("repo", path, content, None).expect("plan");
    ScannedFile {
        relative_path: path.to_string(),
        content_sha256: ContentHash::of(content.as_bytes()),
        size_bytes: content.len() as i64,
        calls,
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

fn assert_converged(client: &mut Client, source_id: i64, scan: &[ScannedFile]) {
    let manifest = load_manifest(client, source_id).expect("load manifest");
    let replay = reconcile(&manifest, scan, LAYOUT_VERSION);
    assert!(
        replay.is_empty(),
        "expected a zero-op replay, got {replay:?}"
    );
    let summary =
        apply_reconciliation(client, source_id, &replay, Utc::now()).expect("apply empty");
    assert_eq!(summary.jobs_created, 0, "empty replay created jobs");
}

#[test]
fn one_root_convergence_and_lease_exclusivity() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
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
    migrate(&mut client).expect("migrate");
    truncate(&mut client);

    // --- Lease exclusivity -------------------------------------------------
    let lease_source = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "engram-test",
            "lease",
            "/tmp/lease-root",
            "engram-test-docs",
            &["**/*.md"],
        ),
    )
    .expect("source");
    let lease_scan = vec![scanned(
        "lease.md",
        "# Lease\n\nA single section long enough for one retain job.",
    )];
    let lease_manifest = load_manifest(&mut client, lease_source).expect("manifest");
    let lease_plan = reconcile(&lease_manifest, &lease_scan, LAYOUT_VERSION);
    let lease_summary =
        apply_reconciliation(&mut client, lease_source, &lease_plan, Utc::now()).expect("apply");
    assert_eq!(lease_summary.jobs_created, 1);

    let now = Utc::now();
    let claimed = claim_job(
        &mut client,
        "engram-test-docs",
        "worker-a",
        Duration::seconds(3_600),
        now,
    )
    .expect("claim")
    .expect("one pending job");
    assert_eq!(claimed.kind, "retain");
    assert_eq!(claimed.payload["document_id"], "repo--lease");
    let second = claim_job(
        &mut client,
        "engram-test-docs",
        "worker-b",
        Duration::seconds(3_600),
        now,
    )
    .expect("second claim");
    assert!(
        second.is_none(),
        "a leased job must not be claimable by another owner"
    );
    let owner: String = client
        .query_one(
            "SELECT owner FROM engram_ingest.leases WHERE job_id = $1",
            &[&claimed.job_id],
        )
        .expect("lease row")
        .get(0);
    assert_eq!(owner, "worker-a");
    assert!(matches!(
        heartbeat_job(
            &mut client,
            claimed.job_id,
            "stale-token",
            Duration::seconds(30),
            now
        ),
        Err(StoreError::LeaseNotHeld)
    ));
    let extended = heartbeat_job(
        &mut client,
        claimed.job_id,
        &claimed.token,
        Duration::seconds(7_200),
        now + Duration::seconds(1),
    )
    .expect("heartbeat");
    assert!(extended > claimed.expires_at);

    // Simulate a dead worker: the lease expires and startup recovery requeues
    // the job for another owner.
    let recovered = recover_expired_leases(&mut client, now + Duration::seconds(7_201))
        .expect("recover expired");
    assert_eq!(recovered.requeued, 1);
    let (state, class): (String, Option<String>) = {
        let row = client
            .query_one(
                "SELECT state, last_error_class FROM engram_ingest.jobs WHERE id = $1",
                &[&claimed.job_id],
            )
            .expect("job row");
        (row.get(0), row.get(1))
    };
    assert_eq!(state, "pending");
    assert_eq!(class.as_deref(), Some("transient"));
    let reclaimed = claim_job(
        &mut client,
        "engram-test-docs",
        "worker-b",
        Duration::seconds(3_600),
        now + Duration::seconds(7_202),
    )
    .expect("reclaim")
    .expect("requeued job");
    assert_eq!(reclaimed.job_id, claimed.job_id);
    complete_job(
        &mut client,
        reclaimed.job_id,
        &reclaimed.token,
        now + Duration::seconds(7_203),
    )
    .expect("complete");
    let state: String = client
        .query_one(
            "SELECT state FROM engram_ingest.jobs WHERE id = $1",
            &[&claimed.job_id],
        )
        .expect("job row")
        .get(0);
    assert_eq!(state, "succeeded");

    // --- One-root convergence ---------------------------------------------
    truncate(&mut client);
    let source_id = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "engram-test",
            "docs",
            "/tmp/docs-root",
            "engram-test-docs",
            &["**/*.md"],
        ),
    )
    .expect("source");

    let original = "# A\n\nAlpha body long enough for this section.\n\n## B\n\nBeta body long enough for this section.";
    let initial_scan = vec![scanned("docs/a.md", original)];
    let manifest = load_manifest(&mut client, source_id).expect("manifest");
    let plan = reconcile(&manifest, &initial_scan, LAYOUT_VERSION);
    assert_eq!(plan.retain.len(), 1);
    assert_eq!(plan.retain[0].calls.len(), 2);
    let summary =
        apply_reconciliation(&mut client, source_id, &plan, Utc::now()).expect("initial apply");
    assert_eq!(summary.retained_files, 1);
    assert_eq!(summary.retained_chunks, 2);
    assert_eq!(summary.jobs_created, 2);
    assert_converged(&mut client, source_id, &initial_scan);

    // Re-applying the same reconciliation (lost commit acknowledgement) must
    // not duplicate jobs or tombstones.
    let repeated =
        apply_reconciliation(&mut client, source_id, &plan, Utc::now()).expect("reapply");
    assert_eq!(repeated.jobs_created, 0);
    let jobs: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs WHERE source_id = $1",
            &[&source_id],
        )
        .expect("job count")
        .get(0);
    assert_eq!(jobs, 2);

    // Edit one section: the Python flow re-emits every chunk of the file.
    let edited = "# A\n\nAlpha body edited but still long enough.\n\n## B\n\nBeta body long enough for this section.";
    let edited_scan = vec![scanned("docs/a.md", edited)];
    let manifest = load_manifest(&mut client, source_id).expect("manifest");
    let plan = reconcile(&manifest, &edited_scan, LAYOUT_VERSION);
    assert_eq!(plan.retain.len(), 1);
    assert_eq!(plan.retain[0].calls.len(), 2);
    assert!(plan.deletes.is_empty());
    apply_reconciliation(&mut client, source_id, &plan, Utc::now()).expect("edit apply");
    assert_converged(&mut client, source_id, &edited_scan);

    // Remove section B: retain the remaining chunk and tombstone the orphan.
    let trimmed = "# A\n\nAlpha body edited but still long enough.";
    let trimmed_scan = vec![scanned("docs/a.md", trimmed)];
    let manifest = load_manifest(&mut client, source_id).expect("manifest");
    let plan = reconcile(&manifest, &trimmed_scan, LAYOUT_VERSION);
    assert_eq!(plan.retain.len(), 1);
    assert_eq!(plan.deletes.len(), 1);
    let summary =
        apply_reconciliation(&mut client, source_id, &plan, Utc::now()).expect("trim apply");
    assert_eq!(summary.deleted_chunks, 1);
    let tombstone_reasons: Vec<String> = client
        .query(
            "SELECT reason FROM engram_ingest.tombstones WHERE source_id = $1",
            &[&source_id],
        )
        .expect("tombstones")
        .into_iter()
        .map(|row| row.get(0))
        .collect();
    assert_eq!(tombstone_reasons, vec!["chunk_removed"]);
    assert_converged(&mut client, source_id, &trimmed_scan);

    // Remove the file: every remaining chunk and the document are tombstoned.
    // Drain earlier test jobs so the delete job is the only claimable one.
    client
        .execute(
            "UPDATE engram_ingest.jobs SET state = 'succeeded' WHERE state = 'pending'",
            &[],
        )
        .expect("drain jobs");
    let manifest = load_manifest(&mut client, source_id).expect("manifest");
    let plan = reconcile(&manifest, &[], LAYOUT_VERSION);
    assert_eq!(plan.removed_files, vec!["docs/a.md"]);
    assert_eq!(plan.deletes.len(), 1);
    let summary =
        apply_reconciliation(&mut client, source_id, &plan, Utc::now()).expect("delete apply");
    assert_eq!(summary.tombstoned_files, 1);
    let state: String = client
        .query_one(
            "SELECT state FROM engram_ingest.documents WHERE source_id = $1 AND relative_path = 'docs/a.md'",
            &[&source_id],
        )
        .expect("document")
        .get(0);
    assert_eq!(state, "tombstoned");
    let delete_job = claim_job(
        &mut client,
        "engram-test-docs",
        "worker-delete",
        Duration::seconds(60),
        Utc::now(),
    )
    .expect("claim delete")
    .expect("delete job");
    assert_eq!(delete_job.kind, "delete");
    complete_job(
        &mut client,
        delete_job.job_id,
        &delete_job.token,
        Utc::now(),
    )
    .expect("complete delete");
    let deleted_document_id = delete_job.payload["document_id"]
        .as_str()
        .expect("delete payload document id")
        .to_string();
    let sink_deleted: Option<chrono::DateTime<Utc>> = client
        .query_one(
            "SELECT sink_deleted_at FROM engram_ingest.tombstones
              WHERE source_id = $1 AND hindsight_document_id = $2",
            &[&source_id, &deleted_document_id],
        )
        .expect("tombstone row")
        .get(0);
    assert!(sink_deleted.is_some());
    assert_converged(&mut client, source_id, &[]);

    // --- Duplicate headings and cross-project isolation --------------------
    truncate(&mut client);
    let duplicate_source = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "engram-test",
            "dup",
            "/tmp/dup-root",
            "engram-test-docs",
            &["**/*.md"],
        ),
    )
    .expect("source");
    let duplicate_scan = vec![scanned(
        "dup.md",
        "Preamble long enough.\n\n## Notes\n\nFirst notes body.\n\n## Notes\n\nSecond notes body.",
    )];
    let manifest = load_manifest(&mut client, duplicate_source).expect("manifest");
    let plan = reconcile(&manifest, &duplicate_scan, LAYOUT_VERSION);
    assert_eq!(plan.retain[0].calls.len(), 2);
    apply_reconciliation(&mut client, duplicate_source, &plan, Utc::now()).expect("dup apply");
    assert_converged(&mut client, duplicate_source, &duplicate_scan);

    let source_a = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "project-a",
            "repo",
            "/tmp/a",
            "project-a-docs",
            &["**/*.md"],
        ),
    )
    .expect("source a");
    let source_b = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "project-b",
            "repo",
            "/tmp/b",
            "project-b-docs",
            &["**/*.md"],
        ),
    )
    .expect("source b");
    let shared = vec![scanned("a.md", "# A\n\nShared path body long enough.")];
    for source in [source_a, source_b] {
        let manifest = load_manifest(&mut client, source).expect("manifest");
        let plan = reconcile(&manifest, &shared, LAYOUT_VERSION);
        apply_reconciliation(&mut client, source, &plan, Utc::now()).expect("apply");
        assert_converged(&mut client, source, &shared);
    }
    let rows: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.chunks
              WHERE hindsight_document_id = 'repo--a'",
            &[],
        )
        .expect("chunk count")
        .get(0);
    assert_eq!(rows, 2, "chunk rows must stay source-scoped");

    let manifest = load_manifest(&mut client, source_a).expect("manifest a");
    let plan = reconcile(&manifest, &[], LAYOUT_VERSION);
    apply_reconciliation(&mut client, source_a, &plan, Utc::now()).expect("delete a");
    assert_converged(&mut client, source_a, &[]);
    assert_converged(&mut client, source_b, &shared);

    // --- Delete, restore, delete again ------------------------------------
    truncate(&mut client);
    let restore_source = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "engram-test",
            "restore",
            "/tmp/restore",
            "engram-test-docs",
            &["**/*.md"],
        ),
    )
    .expect("source");
    let both = "# A\n\nAlpha body long enough.\n\n## B\n\nBeta body long enough.";
    let only_a = "# A\n\nAlpha body long enough.";
    let scan_both = vec![scanned("restore.md", both)];
    let manifest = load_manifest(&mut client, restore_source).expect("manifest");
    let plan = reconcile(&manifest, &scan_both, LAYOUT_VERSION);
    apply_reconciliation(&mut client, restore_source, &plan, Utc::now()).expect("both");
    assert_converged(&mut client, restore_source, &scan_both);

    let scan_only_a = vec![scanned("restore.md", only_a)];
    let manifest = load_manifest(&mut client, restore_source).expect("manifest");
    let plan = reconcile(&manifest, &scan_only_a, LAYOUT_VERSION);
    assert_eq!(plan.deletes.len(), 1);
    apply_reconciliation(&mut client, restore_source, &plan, Utc::now()).expect("only a");
    assert_converged(&mut client, restore_source, &scan_only_a);

    let manifest = load_manifest(&mut client, restore_source).expect("manifest");
    let plan = reconcile(&manifest, &scan_both, LAYOUT_VERSION);
    assert_eq!(plan.retain[0].calls.len(), 2);
    let summary =
        apply_reconciliation(&mut client, restore_source, &plan, Utc::now()).expect("restore");
    // Both chunks are re-emitted (file-scoped, and section A's trailing text
    // changed when B was removed); the restored chunk also needs a new job.
    assert_eq!(summary.jobs_created, 2);
    let generation: i32 = client
        .query_one(
            "SELECT generation FROM engram_ingest.chunks
              WHERE source_id = $1 AND chunk_key <> ''",
            &[&restore_source],
        )
        .expect("restored chunk")
        .get(0);
    assert_eq!(generation, 2);
    let retain_jobs: i64 = client
        .query_one(
            "SELECT count(*)
               FROM engram_ingest.jobs j
               JOIN engram_ingest.chunks c
                 ON c.source_id = j.source_id
                AND c.hindsight_document_id = j.hindsight_document_id
              WHERE c.source_id = $1
                AND c.chunk_key <> ''
                AND j.kind = 'retain'
                AND j.generation = c.generation",
            &[&restore_source],
        )
        .expect("retain jobs")
        .get(0);
    assert_eq!(retain_jobs, 1);
    assert_converged(&mut client, restore_source, &scan_both);

    let manifest = load_manifest(&mut client, restore_source).expect("manifest");
    let plan = reconcile(&manifest, &scan_only_a, LAYOUT_VERSION);
    apply_reconciliation(&mut client, restore_source, &plan, Utc::now()).expect("delete again");
    let tombstone_generations: Vec<i32> = client
        .query(
            "SELECT generation FROM engram_ingest.tombstones
              WHERE source_id = $1 ORDER BY generation",
            &[&restore_source],
        )
        .expect("tombstones")
        .into_iter()
        .map(|row| row.get(0))
        .collect();
    assert_eq!(tombstone_generations, vec![1, 2]);

    // --- Per-document sink ordering across retry backoff -------------------
    truncate(&mut client);
    let order_source = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "engram-test",
            "order",
            "/tmp/order",
            "engram-test-docs",
            &["**/*.md"],
        ),
    )
    .expect("source");
    let order_scan = vec![scanned("order.md", "# A\n\nAlpha body long enough.")];
    let manifest = load_manifest(&mut client, order_source).expect("manifest");
    let plan = reconcile(&manifest, &order_scan, LAYOUT_VERSION);
    apply_reconciliation(&mut client, order_source, &plan, Utc::now()).expect("initial");
    client
        .execute(
            "UPDATE engram_ingest.jobs SET state = 'succeeded' WHERE source_id = $1",
            &[&order_source],
        )
        .expect("drain initial jobs");

    let manifest = load_manifest(&mut client, order_source).expect("manifest");
    let plan = reconcile(&manifest, &[], LAYOUT_VERSION);
    apply_reconciliation(&mut client, order_source, &plan, Utc::now()).expect("remove file");
    let now = Utc::now();
    let delete_job = claim_job(
        &mut client,
        "engram-test-docs",
        "worker-order",
        Duration::seconds(60),
        now,
    )
    .expect("claim delete")
    .expect("delete job");
    assert_eq!(delete_job.kind, "delete");
    fail_job(
        &mut client,
        delete_job.job_id,
        &delete_job.token,
        now,
        "transient",
        "temporary sink failure",
    )
    .expect("fail delete into backoff");

    // Restore while the delete is in backoff. The restore must not jump ahead.
    let manifest = load_manifest(&mut client, order_source).expect("manifest");
    let plan = reconcile(&manifest, &order_scan, LAYOUT_VERSION);
    // Same clock as the claims: only the earlier delete's backoff may block.
    apply_reconciliation(&mut client, order_source, &plan, now).expect("restore");
    let blocked = claim_job(
        &mut client,
        "engram-test-docs",
        "worker-order",
        Duration::seconds(60),
        now + Duration::milliseconds(500),
    )
    .expect("claim blocked");
    assert!(
        blocked.is_none(),
        "a newer retain must not run while an earlier delete is in backoff"
    );
    let ordered = claim_job(
        &mut client,
        "engram-test-docs",
        "worker-order",
        Duration::seconds(60),
        now + Duration::seconds(2),
    )
    .expect("claim ordered")
    .expect("delete should be claimable after backoff");
    assert_eq!(ordered.job_id, delete_job.job_id);
    assert_eq!(ordered.kind, "delete");
    complete_job(
        &mut client,
        ordered.job_id,
        &ordered.token,
        now + Duration::seconds(3),
    )
    .expect("complete ordered delete");
    let next = claim_job(
        &mut client,
        "engram-test-docs",
        "worker-order",
        Duration::seconds(60),
        now + Duration::seconds(3),
    )
    .expect("claim retain")
    .expect("retain after delete");
    assert_eq!(next.kind, "retain");

    // --- A -> B -> A payload return, and stale delete rejection -----------
    truncate(&mut client);
    let edit_source = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "engram-test",
            "edit",
            "/tmp/edit",
            "engram-test-docs",
            &["**/*.md"],
        ),
    )
    .expect("source");
    let edited = "# A\n\nAlpha body long enough.\n\n## B\n\nBeta body edited and long enough.";
    let edit_both = vec![scanned("edit.md", both)];
    let edit_only_a = vec![scanned("edit.md", only_a)];
    let scan_edited = vec![scanned("edit.md", edited)];
    let manifest = load_manifest(&mut client, edit_source).expect("manifest");
    let plan = reconcile(&manifest, &edit_both, LAYOUT_VERSION);
    apply_reconciliation(&mut client, edit_source, &plan, Utc::now()).expect("initial");
    client
        .execute(
            "UPDATE engram_ingest.jobs SET state = 'succeeded' WHERE source_id = $1",
            &[&edit_source],
        )
        .expect("drain");

    // Snapshot for a removal that becomes stale after the edit below.
    let stale_manifest = load_manifest(&mut client, edit_source).expect("manifest");
    let stale_delete = reconcile(&stale_manifest, &edit_only_a, LAYOUT_VERSION);
    assert_eq!(stale_delete.deletes.len(), 1);

    let manifest = load_manifest(&mut client, edit_source).expect("manifest");
    let edit_plan = reconcile(&manifest, &scan_edited, LAYOUT_VERSION);
    apply_reconciliation(&mut client, edit_source, &edit_plan, Utc::now()).expect("edit");
    client
        .execute(
            "UPDATE engram_ingest.jobs SET state = 'succeeded' WHERE source_id = $1",
            &[&edit_source],
        )
        .expect("drain edit");

    let manifest = load_manifest(&mut client, edit_source).expect("manifest");
    let revert_plan = reconcile(&manifest, &edit_both, LAYOUT_VERSION);
    let revert =
        apply_reconciliation(&mut client, edit_source, &revert_plan, Utc::now()).expect("revert");
    assert!(
        revert.jobs_created >= 1,
        "returning to an earlier payload must enqueue a retain"
    );
    let repeated =
        apply_reconciliation(&mut client, edit_source, &revert_plan, Utc::now()).expect("replay");
    assert_eq!(repeated.jobs_created, 0);

    // The stale removal was planned at generation 1; the edit moved B to 2.
    assert!(matches!(
        apply_reconciliation(&mut client, edit_source, &stale_delete, Utc::now()),
        Err(StoreError::StaleReconciliation)
    ));
    let delete_jobs: i64 = client
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs
              WHERE source_id = $1 AND kind = 'delete'",
            &[&edit_source],
        )
        .expect("delete jobs")
        .get(0);
    assert_eq!(
        delete_jobs, 0,
        "a stale delete must not enqueue a sink delete"
    );
    let b_state: String = client
        .query_one(
            "SELECT state FROM engram_ingest.chunks
              WHERE source_id = $1 AND chunk_key <> ''",
            &[&edit_source],
        )
        .expect("chunk b")
        .get(0);
    assert_eq!(b_state, "active");

    truncate(&mut client);
}

#[test]
fn banks_scope_claims_and_recovery() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .expect("database name")
        .get(0);
    assert_eq!(database, "engram_ingest_test");
    client
        .execute("SELECT pg_advisory_lock($1)", &[&918_273_i64])
        .expect("harness lock");
    migrate(&mut client).expect("migrate");
    truncate(&mut client);

    let source_a = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "project-a",
            "repo",
            "/tmp/a",
            "project-a-docs",
            &["**/*.md"],
        ),
    )
    .expect("source a");
    let source_b = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "project-b",
            "repo",
            "/tmp/b",
            "project-b-docs",
            &["**/*.md"],
        ),
    )
    .expect("source b");
    for (source, path, body) in [
        (source_a, "a.md", "# A\n\nAlpha body long enough."),
        (source_b, "b.md", "# B\n\nBeta body long enough."),
    ] {
        let scan = vec![scanned(path, body)];
        let manifest = load_manifest(&mut client, source).expect("manifest");
        let plan = reconcile(&manifest, &scan, LAYOUT_VERSION);
        apply_reconciliation(&mut client, source, &plan, Utc::now()).expect("apply");
    }

    let now = Utc::now();
    let claimed_a = claim_job(
        &mut client,
        "project-a-docs",
        "worker-a",
        Duration::seconds(3600),
        now,
    )
    .expect("claim a")
    .expect("a job");
    assert_eq!(claimed_a.bank_id, "project-a-docs");
    assert_eq!(claimed_a.payload["document_id"], "repo--a");
    assert!(
        claim_job(
            &mut client,
            "project-a-docs",
            "worker-a",
            Duration::seconds(3600),
            now,
        )
        .expect("second a claim")
        .is_none(),
        "bank A must not claim bank B's only job"
    );
    let claimed_b = claim_job(
        &mut client,
        "project-b-docs",
        "worker-b",
        Duration::seconds(3600),
        now,
    )
    .expect("claim b")
    .expect("b job");
    assert_eq!(claimed_b.bank_id, "project-b-docs");
    assert_eq!(claimed_b.payload["document_id"], "repo--b");

    // Bank scope is real: recovering A only touches A.
    let only_a = recover_expired_leases_in_bank(
        &mut client,
        "project-a-docs",
        now + Duration::seconds(4000),
    )
    .expect("recover a");
    assert_eq!(only_a.requeued, 1);
    assert_eq!(only_a.stranded, 0);
    let bank_b_attempts: i32 = client
        .query_one(
            "SELECT attempts FROM engram_ingest.jobs WHERE id = $1",
            &[&claimed_b.job_id],
        )
        .expect("b attempts")
        .get(0);
    assert_eq!(bank_b_attempts, 0, "recovering A must not touch B");

    // A configured bank consumes an attempt; an unconfigured one is merely
    // stranded, so repeated restarts cannot burn its budget.
    let claimed_a = claim_job(
        &mut client,
        "project-a-docs",
        "worker-a",
        Duration::seconds(1),
        now + Duration::seconds(5000),
    )
    .expect("reclaim a")
    .expect("a job");
    let _ = claimed_a;
    let configured = vec!["project-a-docs".to_string()];
    let report =
        recover_expired_leases_for_banks(&mut client, &configured, now + Duration::seconds(8000))
            .expect("configured recovery");
    assert_eq!(report.requeued, 1);
    assert_eq!(report.stranded, 1);
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
    assert_eq!(b_attempts, 0);

    // The global helper keeps normal accounting for every bank.
    let claimed_b = claim_job(
        &mut client,
        "project-b-docs",
        "worker-b",
        Duration::seconds(1),
        now + Duration::seconds(9000),
    )
    .expect("reclaim b")
    .expect("b job");
    let global = recover_expired_leases(&mut client, now + Duration::seconds(9002))
        .expect("global recovery");
    assert_eq!(global.requeued, 1);
    assert!(global.dead == 0);
    let _ = claimed_b;

    // Bank changes are rejected without mutating the existing source.
    let changed = SourceRecord::markdown(
        "project-a",
        "repo",
        "/tmp/a",
        "project-a-other",
        &["**/*.md"],
    );
    assert!(matches!(
        upsert_source(&mut client, &changed),
        Err(StoreError::BankChanged(_))
    ));
    let stored: String = client
        .query_one(
            "SELECT bank_id FROM engram_ingest.sources WHERE id = $1",
            &[&source_a],
        )
        .expect("stored bank")
        .get(0);
    assert_eq!(stored, "project-a-docs");

    let invalid = SourceRecord::markdown("project-a", "repo", "/tmp/a", "bad/bank", &["**/*.md"]);
    assert!(matches!(
        upsert_source(&mut client, &invalid),
        Err(StoreError::InvalidBank(_))
    ));

    truncate(&mut client);
}

#[test]
fn migration_guard_requires_an_explicit_bank_and_is_rerun_safe() {
    let Some(url) = test_url() else {
        eprintln!("ENGRAM_INGEST_TEST_DATABASE_URL is not set; skipping Postgres harness");
        return;
    };
    let mut client = connect(&url).expect("connect");
    let database: String = client
        .query_one("SELECT current_database()", &[])
        .expect("database name")
        .get(0);
    assert_eq!(
        database, "engram_ingest_test",
        "refusing to migrate a database that is not engram_ingest_test"
    );
    client
        .execute("SELECT pg_advisory_lock($1)", &[&918_273_i64])
        .expect("harness lock");
    migrate(&mut client).expect("migrate");
    truncate(&mut client);

    let source = upsert_source(
        &mut client,
        &SourceRecord::markdown(
            "project-a",
            "repo",
            "/tmp/a",
            "project-a-docs",
            &["**/*.md"],
        ),
    )
    .expect("source");
    let scan = vec![scanned("a.md", "# A\n\nAlpha body long enough.")];
    let manifest = load_manifest(&mut client, source).expect("manifest");
    let plan = reconcile(&manifest, &scan, LAYOUT_VERSION);
    apply_reconciliation(&mut client, source, &plan, Utc::now()).expect("apply");

    // Simulate a v2 ledger that has v3 columns but no explicit bank.
    client
        .batch_execute(
            "ALTER TABLE engram_ingest.sources ALTER COLUMN bank_id DROP NOT NULL;
             ALTER TABLE engram_ingest.jobs ALTER COLUMN bank_id DROP NOT NULL;
             UPDATE engram_ingest.sources SET bank_id = NULL;
             UPDATE engram_ingest.jobs SET bank_id = NULL;
             DELETE FROM engram_ingest.schema_migrations WHERE version = 3",
        )
        .expect("simulate v2");
    let error = migrate(&mut client).expect_err("guard must reject a null bank");
    assert!(
        matches!(error, StoreError::MigrationGuard(_)),
        "unexpected error: {error}"
    );
    assert!(error.to_string().contains("set it explicitly"), "{error}");

    client
        .execute(
            "UPDATE engram_ingest.sources SET bank_id = 'project-a-docs' WHERE id = $1",
            &[&source],
        )
        .expect("explicit bank");
    migrate(&mut client).expect("rerun after explicit bank");
    let not_null: bool = client
        .query_one(
            "SELECT bool_and(a.attnotnull)
               FROM pg_attribute a
               JOIN pg_class t ON t.oid = a.attrelid
               JOIN pg_namespace n ON n.oid = t.relnamespace
              WHERE n.nspname = 'engram_ingest'
                AND t.relname IN ('sources', 'jobs')
                AND a.attname = 'bank_id'",
            &[],
        )
        .expect("attnotnull")
        .get(0);
    assert!(not_null);
    let job_bank: String = client
        .query_one(
            "SELECT bank_id FROM engram_ingest.jobs WHERE source_id = $1",
            &[&source],
        )
        .expect("job bank")
        .get(0);
    assert_eq!(job_bank, "project-a-docs");

    truncate(&mut client);
}
