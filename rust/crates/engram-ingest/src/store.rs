//! Postgres implementation of the ingestion ledger.
//!
//! The store is synchronous: ingestion workers own a `postgres::Client` and
//! call these functions from their worker thread. All multi-row mutations run
//! in one transaction, and job claims use `FOR UPDATE SKIP LOCKED`.

use std::collections::BTreeSet;

use chrono::{DateTime, Duration, Utc};
use postgres::{Client, NoTls};
use serde_json::Value;

use crate::hashing::{ContentHash, HashError};
use crate::jobs::RETRY_BASE_MS;
use crate::ledger::{
    is_valid_bank_id, Manifest, ManifestChunk, ManifestDocument, RecordState, SourceRecord,
    MIGRATIONS,
};
use crate::reconcile::Reconciliation;

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error(transparent)]
    Postgres(#[from] postgres::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Hash(#[from] HashError),
    #[error("the job lease is not held by this owner")]
    LeaseNotHeld,
    #[error("unknown ledger state: {0}")]
    InvalidState(String),
    #[error("ledger schema is missing {0}; recreate the unreleased engram_ingest schema")]
    MigrationMismatch(String),
    #[error("ledger schema guard: {0}")]
    MigrationGuard(String),
    #[error("source bank id {0:?} is not a valid Hindsight bank id")]
    InvalidBank(String),
    #[error("source bank id cannot change from {0:?} to another bank")]
    BankChanged(String),
    #[error("scan has conflicting live paths for document id {0}")]
    ScanConflict(String),
    #[error("reconciliation is based on an obsolete source revision; reload the manifest")]
    StaleReconciliation,
    #[error("the provider snapshot predates a newer ledger commit for this source")]
    StaleSnapshot,
}

/// Recovery selection window. Startup and runtime passes use the same value so
/// the pagination loop and SQL limits cannot drift apart.
pub const RECOVERY_BATCH: usize = 500;

/// Counts from one recovery pass. `stranded` leases belong to a bank that is
/// not configured in the running worker; they are requeued without consuming an
/// attempt so repeated restarts cannot exhaust their retry budget.
#[non_exhaustive]
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RecoveryReport {
    pub requeued: usize,
    pub dead: usize,
    pub stranded: usize,
    pub scanned: usize,
}

impl RecoveryReport {
    pub fn is_empty(self) -> bool {
        self.requeued == 0 && self.dead == 0 && self.stranded == 0
    }

    pub fn merge(&mut self, other: Self) {
        self.requeued += other.requeued;
        self.dead += other.dead;
        self.stranded += other.stranded;
        self.scanned += other.scanned;
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailDisposition {
    RetryScheduled,
    Dead,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ApplySummary {
    pub retained_files: usize,
    pub retained_chunks: usize,
    pub deleted_chunks: usize,
    pub tombstoned_files: usize,
    pub jobs_created: usize,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ClaimedJob {
    pub job_id: i64,
    pub owner: String,
    pub token: String,
    pub bank_id: String,
    pub kind: String,
    pub payload: Value,
    pub expires_at: DateTime<Utc>,
}

pub fn connect(url: &str) -> Result<Client, StoreError> {
    Ok(Client::connect(url, NoTls)?)
}

pub fn migrate(client: &mut Client) -> Result<(), StoreError> {
    // The v3 DDL and the NOT NULL guard take ACCESS EXCLUSIVE locks. A live
    // writer may briefly block them, so migrations get a generous lock and
    // statement budget separate from the worker statement timeouts.
    client.batch_execute("SET lock_timeout = '60s'; SET statement_timeout = 0")?;
    let mut transaction = client.transaction()?;
    transaction.execute("SELECT pg_advisory_xact_lock($1)", &[&918_274_i64])?;
    transaction.batch_execute(
        "CREATE SCHEMA IF NOT EXISTS engram_ingest;
         CREATE TABLE IF NOT EXISTS engram_ingest.schema_migrations (
             version bigint PRIMARY KEY,
             applied_at timestamptz NOT NULL DEFAULT now()
         );",
    )?;
    let applied: Vec<i64> = transaction
        .query("SELECT version FROM engram_ingest.schema_migrations", &[])?
        .into_iter()
        .map(|row| row.get(0))
        .collect();
    let known: std::collections::BTreeSet<i64> = MIGRATIONS
        .iter()
        .map(|migration| migration.version)
        .collect();
    if let Some(unknown) = applied.iter().find(|version| !known.contains(version)) {
        return Err(StoreError::MigrationMismatch(format!(
            "database records unembedded migration version {unknown}"
        )));
    }
    for migration in MIGRATIONS {
        if applied.contains(&migration.version) {
            continue;
        }
        transaction.batch_execute(migration.sql)?;
        transaction.execute(
            "INSERT INTO engram_ingest.schema_migrations (version) VALUES ($1)",
            &[&migration.version],
        )?;
    }
    transaction.commit()?;

    // v3 deliberately adds the bank columns as nullable so this guard can fail
    // cleanly when an existing source has no explicit bank. The operator sets
    // it in their own connection and reruns migrate().
    enforce_bank_columns(client)?;

    // `CREATE TABLE IF NOT EXISTS` cannot upgrade a table created by an
    // earlier, unreleased shape of this migration. Fail loudly instead of
    // failing later in `load_manifest`.
    for (table, column) in [
        ("chunks", "source_id"),
        ("chunks", "generation"),
        ("chunks", "retain_revision"),
        ("chunks", "last_retain_sha256"),
        ("jobs", "dedupe_key"),
        ("jobs", "generation"),
        ("jobs", "last_error_class"),
        ("jobs", "bank_id"),
        ("leases", "token"),
        ("sources", "revision"),
        ("sources", "last_plan_fingerprint"),
        ("sources", "bank_id"),
        ("sources", "source_kind"),
        ("issue_cursors", "etag"),
        ("issue_cursors", "fingerprint"),
        ("issue_cursors", "status"),
        ("tombstones", "generation"),
    ] {
        let exists: bool = client
            .query_one(
                "SELECT EXISTS (
                     SELECT 1 FROM information_schema.columns
                      WHERE table_schema = 'engram_ingest'
                        AND table_name = $1 AND column_name = $2)",
                &[&table, &column],
            )?
            .get(0);
        if !exists {
            return Err(StoreError::MigrationMismatch(format!("{table}.{column}")));
        }
    }
    // Column presence is not enough: dedupe and isolation rely on the unique
    // constraints and the partial lease index, which `CREATE TABLE IF NOT
    // EXISTS` does not add to a pre-existing table.
    for (table, constraint) in [
        ("documents", "documents_source_path_key"),
        ("chunks", "chunks_source_document_key"),
        ("chunks", "chunks_document_chunk_key"),
        ("jobs", "jobs_source_dedupe_key"),
        ("sources", "sources_bank_id_format"),
        ("sources", "sources_source_kind_check"),
        ("tombstones", "tombstones_reason_check"),
        ("jobs", "jobs_bank_id_format"),
        ("tombstones", "tombstones_generation_key"),
    ] {
        let exists: bool = client
            .query_one(
                "SELECT EXISTS (
                     SELECT 1 FROM pg_constraint c
                       JOIN pg_class t ON t.oid = c.conrelid
                       JOIN pg_namespace n ON n.oid = t.relnamespace
                      WHERE n.nspname = 'engram_ingest'
                        AND t.relname = $1 AND c.conname = $2)",
                &[&table, &constraint],
            )?
            .get(0);
        if !exists {
            return Err(StoreError::MigrationMismatch(format!(
                "{table} constraint {constraint}"
            )));
        }
    }
    for table in ["sources", "jobs"] {
        let not_null: bool = client
            .query_one(
                "SELECT a.attnotnull
                   FROM pg_attribute a
                   JOIN pg_class t ON t.oid = a.attrelid
                   JOIN pg_namespace n ON n.oid = t.relnamespace
                  WHERE n.nspname = 'engram_ingest'
                    AND t.relname = $1 AND a.attname = 'bank_id'",
                &[&table],
            )?
            .get(0);
        if !not_null {
            return Err(StoreError::MigrationMismatch(format!(
                "{table}.bank_id must be NOT NULL"
            )));
        }
    }
    for index in [
        "jobs_bank_pending_idx",
        "jobs_one_leased_per_bank_document_idx",
    ] {
        let exists: bool = client
            .query_one(
                "SELECT EXISTS (
                     SELECT 1 FROM pg_indexes
                      WHERE schemaname = 'engram_ingest'
                        AND indexname = $1)",
                &[&index],
            )?
            .get(0);
        if !exists {
            return Err(StoreError::MigrationMismatch(index.to_string()));
        }
    }
    let lease_index_shape: bool = client
        .query_one(
            r#"SELECT COALESCE((
                   SELECT i.indisunique
                      AND i.indisvalid
                      AND i.indnkeyatts = 2
                      AND i.indrelid = 'engram_ingest.jobs'::regclass
                      AND pg_get_expr(i.indpred, i.indrelid) = '(state = ''leased''::text)'
                      AND (
                          SELECT array_agg(a.attname ORDER BY k.ord)
                            FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord)
                            JOIN pg_attribute a
                              ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                           WHERE k.ord <= i.indnkeyatts
                      ) = ARRAY['bank_id', 'hindsight_document_id']::name[]
                     FROM pg_index i
                     JOIN pg_class c ON c.oid = i.indexrelid
                     JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'engram_ingest'
                      AND c.relname = 'jobs_one_leased_per_bank_document_idx'
               ), false)"#,
            &[],
        )?
        .get(0);
    if !lease_index_shape {
        return Err(StoreError::MigrationMismatch(
            "jobs_one_leased_per_bank_document_idx shape".to_string(),
        ));
    }
    Ok(())
}

/// Add the v3 bank columns as nullable, then make them NOT NULL once every
/// existing row has an explicit value. The fast path skips the table locks on
/// every later startup.
fn enforce_bank_columns(client: &mut Client) -> Result<(), StoreError> {
    let ready: bool = client
        .query_one(
            "SELECT COALESCE(bool_and(a.attnotnull), false)
               FROM pg_attribute a
               JOIN pg_class t ON t.oid = a.attrelid
               JOIN pg_namespace n ON n.oid = t.relnamespace
              WHERE n.nspname = 'engram_ingest'
                AND t.relname IN ('sources', 'jobs')
                AND a.attname = 'bank_id'
                AND NOT a.attisdropped",
            &[],
        )?
        .get(0);
    if ready {
        return Ok(());
    }

    let mut transaction = client.transaction()?;
    transaction.batch_execute("SET LOCAL lock_timeout = '60s'")?;
    transaction.execute("SELECT pg_advisory_xact_lock($1)", &[&918_274_i64])?;
    transaction.batch_execute(
        "LOCK TABLE engram_ingest.sources, engram_ingest.jobs IN ACCESS EXCLUSIVE MODE",
    )?;

    let null_sources: i64 = transaction
        .query_one(
            "SELECT count(*) FROM engram_ingest.sources WHERE bank_id IS NULL",
            &[],
        )?
        .get(0);
    if null_sources > 0 {
        transaction.rollback()?;
        return Err(StoreError::MigrationGuard(format!(
            "sources.bank_id is unset for {null_sources} source(s); set it explicitly, then rerun migrate()"
        )));
    }

    // The bank-wide unique index may block the bank backfill if two sources
    // produce the same bank document. Drop it, backfill, dedupe, then rebuild.
    transaction.batch_execute(
        "DROP INDEX IF EXISTS engram_ingest.jobs_one_leased_per_bank_document_idx",
    )?;
    let backfill = transaction.execute(
        "UPDATE engram_ingest.jobs j
            SET bank_id = s.bank_id
           FROM engram_ingest.sources s
          WHERE j.source_id = s.id
            AND j.bank_id IS NULL",
        &[],
    );
    if let Err(error) = backfill {
        if error.code() == Some(&postgres::error::SqlState::UNIQUE_VIOLATION) {
            transaction.rollback()?;
            return Err(StoreError::MigrationGuard(
                "duplicate leased jobs for one bank document; rerun migrate() to requeue the surplus"
                    .to_string(),
            ));
        }
        return Err(StoreError::Postgres(error));
    }
    let null_jobs: i64 = transaction
        .query_one(
            "SELECT count(*) FROM engram_ingest.jobs WHERE bank_id IS NULL",
            &[],
        )?
        .get(0);
    if null_jobs > 0 {
        transaction.rollback()?;
        return Err(StoreError::MigrationGuard(format!(
            "jobs.bank_id is unset for {null_jobs} job(s); rerun migrate() after sources.bank_id is set"
        )));
    }

    // v4 could not build the bank-wide lease index while bank ids were NULL.
    // Now that the backfill is complete, refuse to revoke live duplicate
    // leases (that would let a stale in-flight write overtake a requeued job).
    let live_duplicates: i64 = transaction
        .query_one(
            "SELECT count(*) FROM (
                 SELECT 1
                   FROM engram_ingest.jobs j
                   JOIN engram_ingest.leases l ON l.job_id = j.id
                  WHERE j.state = 'leased'
                    AND l.expires_at > now()
                  GROUP BY j.bank_id, j.hindsight_document_id
                 HAVING count(*) > 1
             ) duplicates",
            &[],
        )?
        .get(0);
    if live_duplicates > 0 {
        transaction.rollback()?;
        return Err(StoreError::MigrationGuard(format!(
            "live duplicate leases exist for {live_duplicates} bank document(s); stop ingest workers, let leases expire or drain them, then rerun migrate()"
        )));
    }

    transaction.batch_execute(
        "WITH ranked AS (
             SELECT j.id, l.expires_at,
                    row_number() OVER (
                        PARTITION BY j.bank_id, j.hindsight_document_id
                        ORDER BY (l.expires_at > now()) DESC, j.id ASC
                    ) AS rank
               FROM engram_ingest.jobs j
               JOIN engram_ingest.leases l ON l.job_id = j.id
              WHERE j.state = 'leased'
         ),
         deleted AS (
             DELETE FROM engram_ingest.leases l
              USING ranked r
              WHERE l.job_id = r.id AND r.rank > 1 AND r.expires_at <= now()
             RETURNING l.job_id
         )
         UPDATE engram_ingest.jobs j
            SET state = 'pending', run_after = now(),
                last_error = 'duplicate lease requeued by bank-document migration',
                last_error_class = 'transient', updated_at = now()
           FROM deleted d
          WHERE j.id = d.job_id;
         CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_leased_per_bank_document_idx
             ON engram_ingest.jobs (bank_id, hindsight_document_id)
             WHERE state = 'leased';
         DROP INDEX IF EXISTS engram_ingest.jobs_one_leased_per_document_idx;",
    )?;

    let error = transaction
        .batch_execute(
            "ALTER TABLE engram_ingest.sources ALTER COLUMN bank_id SET NOT NULL;
             ALTER TABLE engram_ingest.jobs ALTER COLUMN bank_id SET NOT NULL;",
        )
        .err();
    if let Some(error) = error {
        if error.code() == Some(&postgres::error::SqlState::NOT_NULL_VIOLATION) {
            transaction.rollback()?;
            return Err(StoreError::MigrationGuard(
                "a concurrent writer inserted a null bank id; rerun migrate()".to_string(),
            ));
        }
        return Err(StoreError::Postgres(error));
    }
    transaction.commit()?;
    Ok(())
}

pub fn upsert_source(client: &mut Client, source: &SourceRecord) -> Result<i64, StoreError> {
    if !is_valid_bank_id(&source.bank_id) {
        return Err(StoreError::InvalidBank(source.bank_id.clone()));
    }
    let mut transaction = client.transaction()?;
    let row = transaction.query_opt(
        "INSERT INTO engram_ingest.sources
             (project, tag, root, bank_id, docs_include, docs_exclude, document_format, source_kind)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
         ON CONFLICT (project, tag, root) DO UPDATE SET
             docs_include = EXCLUDED.docs_include,
             docs_exclude = EXCLUDED.docs_exclude,
             document_format = EXCLUDED.document_format,
             source_kind = EXCLUDED.source_kind,
             updated_at = now()
         WHERE engram_ingest.sources.bank_id = EXCLUDED.bank_id
         RETURNING id",
        &[
            &source.project,
            &source.tag,
            &source.root,
            &source.bank_id,
            &source.docs_include,
            &source.docs_exclude,
            &source.document_format,
            &source.source_kind,
        ],
    )?;
    let Some(row) = row else {
        let existing: Option<String> = transaction
            .query_opt(
                "SELECT bank_id FROM engram_ingest.sources
                  WHERE project = $1 AND tag = $2 AND root = $3",
                &[&source.project, &source.tag, &source.root],
            )?
            .map(|row| row.get(0));
        transaction.rollback()?;
        return Err(StoreError::BankChanged(
            existing.unwrap_or_else(|| "<unknown>".to_string()),
        ));
    };
    let id = row.get(0);
    transaction.commit()?;
    Ok(id)
}

pub fn load_source_revision(client: &mut Client, source_id: i64) -> Result<i64, StoreError> {
    Ok(client
        .query_one(
            "SELECT revision FROM engram_ingest.sources WHERE id = $1",
            &[&source_id],
        )?
        .get(0))
}

pub fn load_manifest(client: &mut Client, source_id: i64) -> Result<Manifest, StoreError> {
    let mut manifest = Manifest::default();
    // Documents and chunks must come from one committed snapshot; the default
    // READ COMMITTED would let a concurrent apply split them across two states.
    let mut transaction = client.transaction()?;
    transaction.batch_execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")?;
    manifest.revision = transaction
        .query_one(
            "SELECT revision FROM engram_ingest.sources WHERE id = $1",
            &[&source_id],
        )?
        .get(0);
    let document_rows = transaction.query(
        "SELECT relative_path, content_sha256, size_bytes, layout_version, state
           FROM engram_ingest.documents
          WHERE source_id = $1
          ORDER BY relative_path",
        &[&source_id],
    )?;
    for row in document_rows {
        let relative_path: String = row.get(0);
        let hash: Vec<u8> = row.get(1);
        let state: String = row.get(4);
        manifest.documents.insert(
            relative_path.clone(),
            ManifestDocument {
                relative_path,
                content_sha256: ContentHash::from_bytes(&hash)?,
                size_bytes: row.get(2),
                layout_version: row.get(3),
                state: RecordState::parse(&state)
                    .ok_or_else(|| StoreError::InvalidState(state.clone()))?,
            },
        );
    }

    let chunk_rows = transaction.query(
        "SELECT d.relative_path, c.hindsight_document_id, c.chunk_key,
                c.content_sha256, c.ordinal, c.state, c.generation
           FROM engram_ingest.chunks c
           JOIN engram_ingest.documents d ON d.id = c.document_id
          WHERE d.source_id = $1
          ORDER BY d.relative_path, c.ordinal",
        &[&source_id],
    )?;
    for row in chunk_rows {
        let document_path: String = row.get(0);
        let hindsight_document_id: String = row.get(1);
        let hash: Vec<u8> = row.get(3);
        let state: String = row.get(5);
        manifest.chunks.insert(
            hindsight_document_id.clone(),
            ManifestChunk {
                document_path,
                hindsight_document_id,
                chunk_key: row.get(2),
                content_sha256: ContentHash::from_bytes(&hash)?,
                ordinal: row.get(4),
                generation: row.get(6),
                state: RecordState::parse(&state)
                    .ok_or_else(|| StoreError::InvalidState(state.clone()))?,
            },
        );
    }
    transaction.commit()?;
    Ok(manifest)
}

pub fn apply_reconciliation(
    client: &mut Client,
    source_id: i64,
    reconciliation: &Reconciliation,
    now: DateTime<Utc>,
) -> Result<ApplySummary, StoreError> {
    if reconciliation.is_empty() {
        return Ok(ApplySummary::default());
    }
    if let Some(conflict) = reconciliation.conflicts.first() {
        return Err(StoreError::ScanConflict(
            conflict.hindsight_document_id.clone(),
        ));
    }
    let mut transaction = client.transaction()?;
    let revision_row = transaction.query_one(
        "SELECT revision, last_plan_fingerprint, bank_id
           FROM engram_ingest.sources WHERE id = $1 FOR UPDATE",
        &[&source_id],
    )?;
    let revision: i64 = revision_row.get(0);
    let last_fingerprint: Option<Vec<u8>> = revision_row.get(1);
    let bank_id: String = revision_row.get(2);
    let fingerprint = plan_fingerprint(reconciliation)?;
    if revision != reconciliation.base_revision {
        let is_immediate_replay = revision == reconciliation.base_revision + 1
            && last_fingerprint.as_deref() == Some(fingerprint.as_bytes());
        transaction.rollback()?;
        if is_immediate_replay {
            return Ok(ApplySummary::default());
        }
        return Err(StoreError::StaleReconciliation);
    }
    let mut summary = ApplySummary {
        retained_files: reconciliation.retain.len(),
        retained_chunks: 0,
        deleted_chunks: reconciliation.deletes.len(),
        tombstoned_files: reconciliation.removed_files.len(),
        jobs_created: 0,
    };

    for file in &reconciliation.retain {
        let document_id: i64 = transaction
            .query_one(
                "INSERT INTO engram_ingest.documents
                     (source_id, relative_path, content_sha256, size_bytes, layout_version, state)
                 VALUES ($1, $2, $3, $4, $5, 'active')
                 ON CONFLICT (source_id, relative_path) DO UPDATE SET
                     content_sha256 = EXCLUDED.content_sha256,
                     size_bytes = EXCLUDED.size_bytes,
                     layout_version = EXCLUDED.layout_version,
                     state = 'active',
                     tombstoned_at = NULL,
                     updated_at = $6
                 RETURNING id",
                &[
                    &source_id,
                    &file.relative_path,
                    &file.content_sha256.as_bytes().to_vec(),
                    &file.size_bytes,
                    &reconciliation.layout_version,
                    &now,
                ],
            )?
            .get(0);

        for (ordinal, call) in file.calls.iter().enumerate() {
            let payload = serde_json::json!({
                "document_id": call.document_id,
                "content": call.content,
                "metadata": call.metadata,
                "tags": call.tags,
                "timestamp": call.timestamp,
            });
            let payload_hash = ContentHash::of(&serde_json::to_vec(&payload)?);
            // Generation changes on content/resurrection (delete ordering);
            // retain_revision changes whenever the payload differs from the
            // last applied payload, so A->B->A enqueues the final A while a
            // replay of the same reconciliation stays a no-op.
            let row = transaction.query_one(
                "INSERT INTO engram_ingest.chunks
                     (source_id, document_id, hindsight_document_id, chunk_key,
                      content_sha256, ordinal, generation, retain_revision,
                      last_retain_sha256, state)
                 VALUES ($1, $2, $3, $4, $5, $6, 1, 1, $7, 'active')
                 ON CONFLICT (source_id, hindsight_document_id) DO UPDATE SET
                     document_id = EXCLUDED.document_id,
                     chunk_key = EXCLUDED.chunk_key,
                     content_sha256 = EXCLUDED.content_sha256,
                     ordinal = EXCLUDED.ordinal,
                     generation = CASE
                         WHEN chunks.content_sha256 = EXCLUDED.content_sha256
                              AND chunks.state = 'active'
                         THEN chunks.generation
                         ELSE chunks.generation + 1
                     END,
                     retain_revision = CASE
                         WHEN chunks.last_retain_sha256 IS NOT DISTINCT FROM EXCLUDED.last_retain_sha256
                              AND chunks.state = 'active'
                         THEN chunks.retain_revision
                         ELSE chunks.retain_revision + 1
                     END,
                     last_retain_sha256 = EXCLUDED.last_retain_sha256,
                     state = 'active',
                     tombstoned_at = NULL
                 RETURNING generation, retain_revision",
                &[
                    &source_id,
                    &document_id,
                    &call.document_id,
                    &call.chunk_key,
                    &call.content_sha256.as_bytes().to_vec(),
                    &(ordinal as i32),
                    &payload_hash.as_bytes().to_vec(),
                ],
            )?;
            let generation: i32 = row.get(0);
            let retain_revision: i32 = row.get(1);
            let dedupe_key = format!(
                "retain:{}:{}:{}",
                call.document_id, retain_revision, payload_hash
            );
            let inserted = transaction.execute(
                "INSERT INTO engram_ingest.jobs
                     (source_id, bank_id, kind, hindsight_document_id, dedupe_key, generation, payload, state, run_after)
                 VALUES ($1, $2, 'retain', $3, $4, $5, $6, 'pending', $7)
                 ON CONFLICT (source_id, dedupe_key) DO NOTHING",
                &[
                    &source_id,
                    &bank_id,
                    &call.document_id,
                    &dedupe_key,
                    &generation,
                    &payload,
                    &now,
                ],
            )?;
            summary.retained_chunks += 1;
            summary.jobs_created += inserted as usize;
        }
    }

    for delete in &reconciliation.deletes {
        // A stale plan (loaded before a concurrent retain changed the chunk)
        // must not tombstone the newer generation or queue a sink delete.
        let updated = transaction.execute(
            "UPDATE engram_ingest.chunks
                SET state = 'tombstoned', tombstoned_at = $3
              WHERE source_id = $1 AND hindsight_document_id = $2
                AND generation = $4 AND state = 'active'",
            &[
                &source_id,
                &delete.hindsight_document_id,
                &now,
                &delete.generation,
            ],
        )?;
        if updated == 0 {
            continue;
        }
        transaction.execute(
            "INSERT INTO engram_ingest.tombstones
                 (source_id, document_id, hindsight_document_id, relative_path, reason, generation)
             SELECT $1, c.document_id, c.hindsight_document_id, $3, $4, $5
               FROM engram_ingest.chunks c
              WHERE c.source_id = $1 AND c.hindsight_document_id = $2
             ON CONFLICT (source_id, hindsight_document_id, reason, generation) DO NOTHING",
            &[
                &source_id,
                &delete.hindsight_document_id,
                &delete.relative_path,
                &delete.reason.as_str(),
                &delete.generation,
            ],
        )?;
        let payload = serde_json::json!({
            "document_id": delete.hindsight_document_id,
            "reason": delete.reason.as_str(),
        });
        let dedupe_key = format!(
            "delete:{}:{}:{}",
            delete.hindsight_document_id, delete.generation, delete.content_sha256
        );
        let inserted = transaction.execute(
            "INSERT INTO engram_ingest.jobs
                 (source_id, bank_id, kind, hindsight_document_id, dedupe_key, generation, payload, state, run_after)
             VALUES ($1, $2, 'delete', $3, $4, $5, $6, 'pending', $7)
             ON CONFLICT (source_id, dedupe_key) DO NOTHING",
            &[
                &source_id,
                &bank_id,
                &delete.hindsight_document_id,
                &dedupe_key,
                &delete.generation,
                &payload,
                &now,
            ],
        )?;
        summary.jobs_created += inserted as usize;
    }

    transaction.execute(
        "UPDATE engram_ingest.sources
            SET revision = revision + 1, last_plan_fingerprint = $2, updated_at = $3
          WHERE id = $1",
        &[&source_id, &fingerprint.as_bytes().to_vec(), &now],
    )?;

    for path in &reconciliation.removed_files {
        transaction.execute(
            "UPDATE engram_ingest.documents
                SET state = 'tombstoned', tombstoned_at = $3, updated_at = $3
              WHERE source_id = $1 AND relative_path = $2",
            &[&source_id, path, &now],
        )?;
    }

    transaction.commit()?;
    Ok(summary)
}

fn plan_fingerprint(reconciliation: &Reconciliation) -> Result<ContentHash, StoreError> {
    let retain: Vec<Value> = reconciliation
        .retain
        .iter()
        .map(|file| {
            serde_json::json!({
                "path": file.relative_path,
                "sha": file.content_sha256.to_string(),
                "calls": file.calls.iter().map(|call| serde_json::json!({
                    "document_id": call.document_id,
                    "chunk_key": call.chunk_key,
                    "sha": call.content_sha256.to_string(),
                    "metadata": call.metadata,
                    "tags": call.tags,
                    "timestamp": call.timestamp,
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    let deletes: Vec<Value> = reconciliation
        .deletes
        .iter()
        .map(|delete| {
            serde_json::json!({
                "document_id": delete.hindsight_document_id,
                "chunk_key": delete.chunk_key,
                "generation": delete.generation,
                "sha": delete.content_sha256.to_string(),
                "reason": delete.reason.as_str(),
            })
        })
        .collect();
    let value = serde_json::json!({
        "base_revision": reconciliation.base_revision,
        "layout_version": reconciliation.layout_version,
        "retain": retain,
        "deletes": deletes,
        "removed_files": reconciliation.removed_files,
    });
    Ok(ContentHash::of(&serde_json::to_vec(&value)?))
}

pub fn claim_job(
    client: &mut Client,
    bank_id: &str,
    owner: &str,
    ttl: Duration,
    now: DateTime<Utc>,
) -> Result<Option<ClaimedJob>, StoreError> {
    // A concurrent claim for the same document can lose the partial unique
    // index race; retry so the caller transparently picks another job.
    let mut contention = None;
    for _ in 0..3 {
        match try_claim_job(client, bank_id, owner, ttl, now) {
            Ok(result) => return Ok(result),
            Err(StoreError::Postgres(error))
                if error.code() == Some(&postgres::error::SqlState::UNIQUE_VIOLATION) =>
            {
                contention = Some(StoreError::Postgres(error));
            }
            Err(error) => return Err(error),
        }
    }
    // Persistent contention is a ledger problem, not an empty queue. Surface it
    // so the slot backs off and records a ledger error.
    match contention {
        Some(error) => Err(error),
        None => Ok(None),
    }
}

fn try_claim_job(
    client: &mut Client,
    bank_id: &str,
    owner: &str,
    ttl: Duration,
    now: DateTime<Utc>,
) -> Result<Option<ClaimedJob>, StoreError> {
    let mut transaction = client.transaction()?;
    let row = transaction.query_opt(
        "SELECT j.id, j.kind, j.payload, j.bank_id
           FROM engram_ingest.jobs j
          WHERE j.bank_id = $2
            AND j.state = 'pending' AND j.run_after <= $1
            AND NOT EXISTS (
                SELECT 1
                  FROM engram_ingest.jobs leased
                 WHERE leased.bank_id = j.bank_id
                   AND leased.hindsight_document_id = j.hindsight_document_id
                   AND leased.state = 'leased'
            )
            AND NOT EXISTS (
                SELECT 1
                  FROM engram_ingest.jobs earlier
                 WHERE earlier.bank_id = j.bank_id
                   AND earlier.hindsight_document_id = j.hindsight_document_id
                   AND earlier.id < j.id
                   AND earlier.state IN ('pending', 'leased')
            )
          ORDER BY j.id
          FOR UPDATE SKIP LOCKED
          LIMIT 1",
        &[&now, &bank_id],
    )?;
    let Some(row) = row else {
        transaction.commit()?;
        return Ok(None);
    };
    let job_id: i64 = row.get(0);
    let kind: String = row.get(1);
    let payload: Value = row.get(2);
    let job_bank_id: String = row.get(3);
    let expires_at = now + ttl;
    let token = crate::jobs::new_claim_token();
    transaction.execute(
        "UPDATE engram_ingest.jobs
            SET state = 'leased', updated_at = $2
          WHERE id = $1 AND state = 'pending'",
        &[&job_id, &now],
    )?;
    transaction.execute(
        "INSERT INTO engram_ingest.leases (job_id, owner, token, expires_at, heartbeat_at)
         VALUES ($1, $2, $3, $4, $5)",
        &[&job_id, &owner, &token, &expires_at, &now],
    )?;
    transaction.commit()?;
    Ok(Some(ClaimedJob {
        job_id,
        owner: owner.to_string(),
        token,
        bank_id: job_bank_id,
        kind,
        payload,
        expires_at,
    }))
}

pub fn complete_job(
    client: &mut Client,
    job_id: i64,
    token: &str,
    now: DateTime<Utc>,
) -> Result<(), StoreError> {
    let mut transaction = client.transaction()?;
    let updated = transaction.execute(
        "UPDATE engram_ingest.jobs j
            SET state = 'succeeded',
                updated_at = $3,
                last_sink_operation = CASE WHEN j.kind = 'delete' THEN 'delete' ELSE 'retain' END
           FROM engram_ingest.leases l
          WHERE l.job_id = j.id AND j.id = $1 AND j.state = 'leased' AND l.token = $2
            AND l.expires_at > $3",
        &[&job_id, &token, &now],
    )?;
    if updated == 0 {
        transaction.rollback()?;
        return Err(StoreError::LeaseNotHeld);
    }
    transaction.execute(
        "UPDATE engram_ingest.tombstones t
            SET sink_deleted_at = $2
           FROM engram_ingest.jobs j
          WHERE j.id = $1
            AND j.kind = 'delete'
            AND t.source_id = j.source_id
            AND t.hindsight_document_id = j.hindsight_document_id
            AND t.generation = j.generation
            AND t.sink_deleted_at IS NULL",
        &[&job_id, &now],
    )?;
    transaction.execute(
        "DELETE FROM engram_ingest.leases WHERE job_id = $1 AND token = $2",
        &[&job_id, &token],
    )?;
    transaction.commit()?;
    Ok(())
}

pub fn heartbeat_job(
    client: &mut Client,
    job_id: i64,
    token: &str,
    ttl: Duration,
    now: DateTime<Utc>,
) -> Result<DateTime<Utc>, StoreError> {
    let expires_at = now + ttl;
    let updated = client.execute(
        "UPDATE engram_ingest.leases
            SET expires_at = $3, heartbeat_at = $4
          WHERE job_id = $1 AND token = $2 AND expires_at > $4",
        &[&job_id, &token, &expires_at, &now],
    )?;
    if updated == 0 {
        return Err(StoreError::LeaseNotHeld);
    }
    Ok(expires_at)
}

/// True while the claim's opaque token still owns an unexpired lease. Used
/// immediately before a Hindsight mutation so a paused worker cannot dispatch a
/// stale operation after another worker recovered the job.
pub fn lease_is_active(
    client: &mut Client,
    job_id: i64,
    token: &str,
    now: DateTime<Utc>,
) -> Result<bool, StoreError> {
    let row = client.query_opt(
        "SELECT 1 FROM engram_ingest.leases
          WHERE job_id = $1 AND token = $2 AND expires_at > $3",
        &[&job_id, &token, &now],
    )?;
    Ok(row.is_some())
}

/// Startup recovery across every bank. Configured-bank accounting includes the
/// attempt increment that eventually marks a permanently crashing job `dead`.
pub fn recover_expired_leases(
    client: &mut Client,
    now: DateTime<Utc>,
) -> Result<RecoveryReport, StoreError> {
    recover_expired_leases_filtered(client, now, None, None, None)
}

/// Recovery used by a running worker: only this bank's leases, with normal
/// attempt accounting.
pub fn recover_expired_leases_in_bank(
    client: &mut Client,
    bank_id: &str,
    now: DateTime<Utc>,
) -> Result<RecoveryReport, StoreError> {
    recover_expired_leases_filtered(client, now, Some(bank_id), None, None)
}

/// Startup recovery for a multi-bank worker. Leases for banks that are not in
/// `configured_banks` are requeued without consuming an attempt, so a bank that
/// was temporarily removed from config cannot exhaust its retry budget while no
/// worker is serving it.
pub fn recover_expired_leases_for_banks(
    client: &mut Client,
    configured_banks: &[String],
    now: DateTime<Utc>,
) -> Result<RecoveryReport, StoreError> {
    let configured: BTreeSet<&str> = configured_banks.iter().map(String::as_str).collect();
    recover_expired_leases_filtered(client, now, None, Some(&configured), None)
}

/// Startup recovery restricted to banks that are not configured in this worker.
/// Those leases are stranded: requeue them without consuming an attempt.
pub fn recover_expired_leases_outside_banks(
    client: &mut Client,
    configured_banks: &[String],
    now: DateTime<Utc>,
) -> Result<RecoveryReport, StoreError> {
    let configured: BTreeSet<&str> = configured_banks.iter().map(String::as_str).collect();
    let excluded = configured_banks.to_vec();
    recover_expired_leases_filtered(client, now, None, Some(&configured), Some(&excluded))
}

/// Expired leases still held by banks outside the worker config. Used for the
/// startup stranded gauge after the bounded recovery loop finishes.
pub fn count_stranded_expired_leases(
    client: &mut Client,
    configured_banks: &[String],
    now: DateTime<Utc>,
) -> Result<i64, StoreError> {
    let configured = configured_banks.to_vec();
    let row = client.query_one(
        "SELECT count(*)
           FROM engram_ingest.jobs j
           JOIN engram_ingest.leases l ON l.job_id = j.id
          WHERE j.state = 'leased'
            AND l.expires_at <= $1
            AND j.bank_id <> ALL($2)",
        &[&now, &configured],
    )?;
    Ok(row.get(0))
}

/// Drain counters for the one-shot runtime (`--once`).
///
/// `pending` includes future-dated retries so a normal backoff is waited out
/// rather than reported as a clean drain. `leased` counts every leased job, even
/// an expired one that startup recovery has not reached yet, so a dead-slot
/// backlog cannot look like a clean drain. `dead` counts terminal jobs marked
/// dead since the run started, including jobs that startup recovery or an early
/// worker failure killed before drain polling began; older dead rows from a
/// previous run do not fail this backfill.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct DrainCounts {
    pub pending: u64,
    pub leased: u64,
    pub dead: u64,
}

impl DrainCounts {
    pub fn is_empty(self) -> bool {
        self.pending == 0 && self.leased == 0 && self.dead == 0
    }
}

pub fn count_drain_jobs(
    client: &mut Client,
    banks: &[String],
    since: DateTime<Utc>,
) -> Result<DrainCounts, StoreError> {
    if banks.is_empty() {
        return Ok(DrainCounts::default());
    }
    let banks = banks.to_vec();
    let mut transaction = client.transaction()?;
    transaction.batch_execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")?;
    let pending = transaction.query_one(
        "SELECT count(*)
           FROM engram_ingest.jobs
          WHERE bank_id = ANY($1)
            AND state = 'pending'",
        &[&banks],
    )?;
    let leased = transaction.query_one(
        "SELECT count(*)
           FROM engram_ingest.jobs
          WHERE bank_id = ANY($1)
            AND state = 'leased'",
        &[&banks],
    )?;
    let dead = transaction.query_one(
        "SELECT count(*)
           FROM engram_ingest.jobs
          WHERE bank_id = ANY($2)
            AND state = 'dead'
            AND updated_at >= $1",
        &[&since, &banks],
    )?;
    transaction.commit()?;
    Ok(DrainCounts {
        pending: pending.get::<_, i64>(0).max(0) as u64,
        leased: leased.get::<_, i64>(0).max(0) as u64,
        dead: dead.get::<_, i64>(0).max(0) as u64,
    })
}

fn recover_expired_leases_filtered(
    client: &mut Client,
    now: DateTime<Utc>,
    bank_filter: Option<&str>,
    configured_banks: Option<&BTreeSet<&str>>,
    excluded_banks: Option<&Vec<String>>,
) -> Result<RecoveryReport, StoreError> {
    let mut transaction = client.transaction()?;
    let sql = if bank_filter.is_some() {
        format!(
            "SELECT j.id, j.attempts, j.max_attempts, j.bank_id
               FROM engram_ingest.jobs j
               JOIN engram_ingest.leases l ON l.job_id = j.id
              WHERE j.state = 'leased' AND l.expires_at <= $1 AND j.bank_id = $2
              ORDER BY j.id
              LIMIT {RECOVERY_BATCH}
              FOR UPDATE OF j SKIP LOCKED"
        )
    } else if excluded_banks.is_some() {
        format!(
            "SELECT j.id, j.attempts, j.max_attempts, j.bank_id
               FROM engram_ingest.jobs j
               JOIN engram_ingest.leases l ON l.job_id = j.id
              WHERE j.state = 'leased' AND l.expires_at <= $1
                AND j.bank_id <> ALL($2)
              ORDER BY j.id
              LIMIT {RECOVERY_BATCH}
              FOR UPDATE OF j SKIP LOCKED"
        )
    } else {
        format!(
            "SELECT j.id, j.attempts, j.max_attempts, j.bank_id
               FROM engram_ingest.jobs j
               JOIN engram_ingest.leases l ON l.job_id = j.id
              WHERE j.state = 'leased' AND l.expires_at <= $1
              ORDER BY j.id
              LIMIT {RECOVERY_BATCH}
              FOR UPDATE OF j SKIP LOCKED"
        )
    };
    let rows = if let Some(bank_id) = bank_filter {
        transaction.query(&sql, &[&now, &bank_id])?
    } else if let Some(excluded) = excluded_banks {
        transaction.query(&sql, &[&now, &excluded])?
    } else {
        transaction.query(&sql, &[&now])?
    };
    let mut report = RecoveryReport::default();
    for row in rows {
        report.scanned += 1;
        let job_id: i64 = row.get(0);
        let attempts: i32 = row.get(1);
        let max_attempts: i32 = row.get(2);
        let bank_id: String = row.get(3);
        // A heartbeat may have extended the lease after the SELECT. Only the
        // transaction that actually removes the still-expired lease may requeue
        // the job; otherwise a live worker would lose its lease.
        let deleted = transaction.execute(
            "DELETE FROM engram_ingest.leases
              WHERE job_id = $1 AND expires_at <= $2",
            &[&job_id, &now],
        )?;
        if deleted == 0 {
            continue;
        }

        let stranded = configured_banks.is_some_and(|banks| !banks.contains(bank_id.as_str()));
        if stranded {
            transaction.execute(
                "UPDATE engram_ingest.jobs
                    SET state = 'pending', run_after = $2,
                        last_error = 'lease expired (bank not configured)',
                        last_error_class = 'transient', updated_at = $2
                  WHERE id = $1",
                &[&job_id, &now],
            )?;
            report.stranded += 1;
            continue;
        }

        let next_attempts = attempts + 1;
        let dead = next_attempts >= max_attempts;
        let shift = attempts.clamp(0, 31);
        let run_after = now + Duration::milliseconds(RETRY_BASE_MS.saturating_mul(1_i64 << shift));
        transaction.execute(
            "UPDATE engram_ingest.jobs
                SET state = $2, attempts = $3, run_after = $4,
                    last_error = 'lease expired', last_error_class = 'transient',
                    updated_at = $5
              WHERE id = $1",
            &[
                &job_id,
                &(if dead { "dead" } else { "pending" }),
                &next_attempts,
                &run_after,
                &now,
            ],
        )?;
        if dead {
            report.dead += 1;
        } else {
            report.requeued += 1;
        }
    }
    transaction.commit()?;
    Ok(report)
}

pub fn fail_job(
    client: &mut Client,
    job_id: i64,
    token: &str,
    now: DateTime<Utc>,
    error_class: &str,
    error: &str,
) -> Result<FailDisposition, StoreError> {
    let mut transaction = client.transaction()?;
    let row = transaction.query_opt(
        "SELECT j.attempts, j.max_attempts
           FROM engram_ingest.jobs j
           JOIN engram_ingest.leases l ON l.job_id = j.id
          WHERE j.id = $1 AND j.state = 'leased' AND l.token = $2
            AND l.expires_at > $3
          FOR UPDATE",
        &[&job_id, &token, &now],
    )?;
    let Some(row) = row else {
        transaction.rollback()?;
        return Err(StoreError::LeaseNotHeld);
    };
    let attempts: i32 = row.get(0);
    let max_attempts: i32 = row.get(1);
    let next_attempts = attempts + 1;
    let dead = next_attempts >= max_attempts;
    let shift = attempts.clamp(0, 31);
    let run_after = now + Duration::milliseconds(RETRY_BASE_MS.saturating_mul(1_i64 << shift));
    transaction.execute(
        "UPDATE engram_ingest.jobs
            SET state = $2, attempts = $3, run_after = $4,
                last_error = $5, last_error_class = $6, updated_at = $7
          WHERE id = $1",
        &[
            &job_id,
            &(if dead { "dead" } else { "pending" }),
            &next_attempts,
            &run_after,
            &error,
            &error_class,
            &now,
        ],
    )?;
    transaction.execute(
        "DELETE FROM engram_ingest.leases WHERE job_id = $1 AND token = $2",
        &[&job_id, &token],
    )?;
    transaction.commit()?;
    Ok(if dead {
        FailDisposition::Dead
    } else {
        FailDisposition::RetryScheduled
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct IssueCursor {
    pub etag: Option<String>,
    pub fingerprint: Option<String>,
    pub last_updated: Option<String>,
    pub last_polled_at: Option<DateTime<Utc>>,
    pub item_count: i32,
    pub page_count: i32,
    pub last_error: Option<String>,
    pub status: String,
}

pub fn load_issue_cursor(
    client: &mut Client,
    source_id: i64,
) -> Result<Option<IssueCursor>, StoreError> {
    let row = client.query_opt(
        "SELECT etag, fingerprint, last_updated, last_polled_at, item_count, page_count, last_error, status
           FROM engram_ingest.issue_cursors
          WHERE source_id = $1",
        &[&source_id],
    )?;
    Ok(row.map(|row| IssueCursor {
        etag: row.get(0),
        fingerprint: row.get(1),
        last_updated: row.get(2),
        last_polled_at: row.get(3),
        item_count: row.get(4),
        page_count: row.get(5),
        last_error: row.get(6),
        status: row.get(7),
    }))
}

pub fn save_issue_cursor(
    client: &mut Client,
    source_id: i64,
    cursor: &IssueCursor,
    now: DateTime<Utc>,
) -> Result<(), StoreError> {
    client.execute(
        "INSERT INTO engram_ingest.issue_cursors
             (source_id, etag, fingerprint, last_updated, last_polled_at, item_count, page_count, last_error, status, updated_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
         ON CONFLICT (source_id) DO UPDATE SET
             etag = EXCLUDED.etag,
             fingerprint = EXCLUDED.fingerprint,
             last_updated = EXCLUDED.last_updated,
             last_polled_at = EXCLUDED.last_polled_at,
             item_count = EXCLUDED.item_count,
             page_count = EXCLUDED.page_count,
             last_error = EXCLUDED.last_error,
             status = EXCLUDED.status,
             updated_at = EXCLUDED.updated_at",
        &[
            &source_id,
            &cursor.etag,
            &cursor.fingerprint,
            &cursor.last_updated,
            &cursor.last_polled_at,
            &cursor.item_count,
            &cursor.page_count,
            &cursor.last_error,
            &cursor.status,
            &now,
        ],
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn migration_declares_every_ledger_table() {
        for table in [
            "schema_migrations",
            "sources",
            "documents",
            "chunks",
            "jobs",
            "leases",
            "tombstones",
            "issue_cursors",
        ] {
            assert!(
                MIGRATIONS
                    .iter()
                    .any(|migration| migration.sql.contains(&format!("engram_ingest.{table}"))),
                "missing {table}"
            );
        }
    }

    #[test]
    fn every_migration_version_is_ordered_and_unique() {
        assert!(MIGRATIONS
            .windows(2)
            .all(|pair| pair[0].version < pair[1].version));
        assert_eq!(
            MIGRATIONS.last().map(|migration| migration.version),
            Some(5)
        );
    }

    #[test]
    fn bank_ids_are_path_segment_safe() {
        assert!(is_valid_bank_id("kubernaut-docs"));
        assert!(is_valid_bank_id("hindsight_docs.v2"));
        assert!(!is_valid_bank_id(""));
        assert!(!is_valid_bank_id("with space"));
        assert!(!is_valid_bank_id("slash/inside"));
        assert!(!is_valid_bank_id("."));
        assert!(!is_valid_bank_id(".."));
        assert!(!is_valid_bank_id(&"a".repeat(129)));
    }
}
