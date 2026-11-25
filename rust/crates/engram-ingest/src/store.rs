//! Postgres implementation of the ingestion ledger.
//!
//! The store is synchronous: ingestion workers own a `postgres::Client` and
//! call these functions from their worker thread. All multi-row mutations run
//! in one transaction, and job claims use `FOR UPDATE SKIP LOCKED`.

use chrono::{DateTime, Duration, Utc};
use postgres::{Client, NoTls};
use serde_json::Value;

use crate::hashing::{ContentHash, HashError};
use crate::jobs::RETRY_BASE_MS;
use crate::ledger::{
    Manifest, ManifestChunk, ManifestDocument, RecordState, SourceRecord, LEDGER_MIGRATION,
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
    pub kind: String,
    pub payload: Value,
    pub expires_at: DateTime<Utc>,
}

pub fn connect(url: &str) -> Result<Client, StoreError> {
    Ok(Client::connect(url, NoTls)?)
}

pub fn migrate(client: &mut Client) -> Result<(), StoreError> {
    client.batch_execute(LEDGER_MIGRATION)?;
    // `CREATE TABLE IF NOT EXISTS` cannot upgrade a table created by an
    // earlier, unreleased shape of this migration. Fail loudly instead of
    // failing later in `load_manifest`.
    for (table, column) in [
        ("chunks", "source_id"),
        ("chunks", "generation"),
        ("jobs", "dedupe_key"),
        ("jobs", "generation"),
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
    let lease_index: bool = client
        .query_one(
            "SELECT EXISTS (
                 SELECT 1 FROM pg_indexes
                  WHERE schemaname = 'engram_ingest'
                    AND indexname = 'jobs_one_leased_per_document_idx')",
            &[],
        )?
        .get(0);
    if !lease_index {
        return Err(StoreError::MigrationMismatch(
            "jobs_one_leased_per_document_idx".to_string(),
        ));
    }
    Ok(())
}

pub fn upsert_source(client: &mut Client, source: &SourceRecord) -> Result<i64, StoreError> {
    let row = client.query_one(
        "INSERT INTO engram_ingest.sources
             (project, tag, root, docs_include, docs_exclude, document_format)
         VALUES ($1, $2, $3, $4, $5, $6)
         ON CONFLICT (project, tag, root) DO UPDATE SET
             docs_include = EXCLUDED.docs_include,
             docs_exclude = EXCLUDED.docs_exclude,
             document_format = EXCLUDED.document_format,
             updated_at = now()
         RETURNING id",
        &[
            &source.project,
            &source.tag,
            &source.root,
            &source.docs_include,
            &source.docs_exclude,
            &source.document_format,
        ],
    )?;
    Ok(row.get(0))
}

pub fn load_manifest(client: &mut Client, source_id: i64) -> Result<Manifest, StoreError> {
    let mut manifest = Manifest::default();
    let document_rows = client.query(
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

    let chunk_rows = client.query(
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
    Ok(manifest)
}

pub fn apply_reconciliation(
    client: &mut Client,
    source_id: i64,
    reconciliation: &Reconciliation,
    now: DateTime<Utc>,
) -> Result<ApplySummary, StoreError> {
    let mut transaction = client.transaction()?;
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
            // A re-add of byte-identical content keeps its generation; a new
            // content or a resurrected chunk gets a fresh one so a later
            // delete is not suppressed by the old delete job's dedupe key.
            let generation: i32 = transaction
                .query_one(
                    "INSERT INTO engram_ingest.chunks
                         (source_id, document_id, hindsight_document_id, chunk_key,
                          content_sha256, ordinal, generation, state)
                     VALUES ($1, $2, $3, $4, $5, $6, 1, 'active')
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
                         state = 'active',
                         tombstoned_at = NULL
                     RETURNING generation",
                    &[
                        &source_id,
                        &document_id,
                        &call.document_id,
                        &call.chunk_key,
                        &call.content_sha256.as_bytes().to_vec(),
                        &(ordinal as i32),
                    ],
                )?
                .get(0);
            let payload = serde_json::json!({
                "document_id": call.document_id,
                "content": call.content,
                "metadata": call.metadata,
                "tags": call.tags,
                "timestamp": call.timestamp,
            });
            let dedupe_key = format!(
                "retain:{}:{}:{}",
                call.document_id,
                generation,
                ContentHash::of(&serde_json::to_vec(&payload)?)
            );
            let inserted = transaction.execute(
                "INSERT INTO engram_ingest.jobs
                     (source_id, kind, hindsight_document_id, dedupe_key, generation, payload, state, run_after)
                 VALUES ($1, 'retain', $2, $3, $4, $5, 'pending', $6)
                 ON CONFLICT (source_id, dedupe_key) DO NOTHING",
                &[
                    &source_id,
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
        transaction.execute(
            "UPDATE engram_ingest.chunks
                SET state = 'tombstoned', tombstoned_at = $3
              WHERE source_id = $1 AND hindsight_document_id = $2",
            &[&source_id, &delete.hindsight_document_id, &now],
        )?;
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
                 (source_id, kind, hindsight_document_id, dedupe_key, generation, payload, state, run_after)
             VALUES ($1, 'delete', $2, $3, $4, $5, 'pending', $6)
             ON CONFLICT (source_id, dedupe_key) DO NOTHING",
            &[
                &source_id,
                &delete.hindsight_document_id,
                &dedupe_key,
                &delete.generation,
                &payload,
                &now,
            ],
        )?;
        summary.jobs_created += inserted as usize;
    }

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

pub fn claim_job(
    client: &mut Client,
    owner: &str,
    ttl: Duration,
    now: DateTime<Utc>,
) -> Result<Option<ClaimedJob>, StoreError> {
    // A concurrent claim for the same document can lose the partial unique
    // index race; retry so the caller transparently picks another job.
    for _ in 0..3 {
        match try_claim_job(client, owner, ttl, now) {
            Ok(result) => return Ok(result),
            Err(StoreError::Postgres(error))
                if error.code() == Some(&postgres::error::SqlState::UNIQUE_VIOLATION) => {}
            Err(error) => return Err(error),
        }
    }
    Ok(None)
}

fn try_claim_job(
    client: &mut Client,
    owner: &str,
    ttl: Duration,
    now: DateTime<Utc>,
) -> Result<Option<ClaimedJob>, StoreError> {
    let mut transaction = client.transaction()?;
    let row = transaction.query_opt(
        "SELECT j.id, j.kind, j.payload
           FROM engram_ingest.jobs j
          WHERE j.state = 'pending' AND j.run_after <= $1
            AND NOT EXISTS (
                SELECT 1
                  FROM engram_ingest.jobs leased
                  JOIN engram_ingest.leases l ON l.job_id = leased.id
                 WHERE leased.source_id = j.source_id
                   AND leased.hindsight_document_id = j.hindsight_document_id
                   AND leased.state = 'leased'
            )
            AND NOT EXISTS (
                SELECT 1
                  FROM engram_ingest.jobs earlier
                 WHERE earlier.source_id = j.source_id
                   AND earlier.hindsight_document_id = j.hindsight_document_id
                   AND earlier.id < j.id
                   AND earlier.state IN ('pending', 'leased')
            )
          ORDER BY j.id
          FOR UPDATE SKIP LOCKED
          LIMIT 1",
        &[&now],
    )?;
    let Some(row) = row else {
        transaction.commit()?;
        return Ok(None);
    };
    let job_id: i64 = row.get(0);
    let kind: String = row.get(1);
    let payload: Value = row.get(2);
    let expires_at = now + ttl;
    transaction.execute(
        "UPDATE engram_ingest.jobs
            SET state = 'leased', updated_at = $2
          WHERE id = $1 AND state = 'pending'",
        &[&job_id, &now],
    )?;
    transaction.execute(
        "INSERT INTO engram_ingest.leases (job_id, owner, expires_at, heartbeat_at)
         VALUES ($1, $2, $3, $4)",
        &[&job_id, &owner, &expires_at, &now],
    )?;
    transaction.commit()?;
    Ok(Some(ClaimedJob {
        job_id,
        owner: owner.to_string(),
        kind,
        payload,
        expires_at,
    }))
}

pub fn complete_job(
    client: &mut Client,
    job_id: i64,
    owner: &str,
    now: DateTime<Utc>,
) -> Result<(), StoreError> {
    let mut transaction = client.transaction()?;
    let updated = transaction.execute(
        "UPDATE engram_ingest.jobs j
            SET state = 'succeeded',
                updated_at = $3,
                last_sink_operation = CASE WHEN j.kind = 'delete' THEN 'delete' ELSE 'retain' END
           FROM engram_ingest.leases l
          WHERE l.job_id = j.id AND j.id = $1 AND j.state = 'leased' AND l.owner = $2",
        &[&job_id, &owner, &now],
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
        "DELETE FROM engram_ingest.leases WHERE job_id = $1 AND owner = $2",
        &[&job_id, &owner],
    )?;
    transaction.commit()?;
    Ok(())
}

pub fn heartbeat_job(
    client: &mut Client,
    job_id: i64,
    owner: &str,
    ttl: Duration,
    now: DateTime<Utc>,
) -> Result<DateTime<Utc>, StoreError> {
    let expires_at = now + ttl;
    let updated = client.execute(
        "UPDATE engram_ingest.leases
            SET expires_at = $3, heartbeat_at = $4
          WHERE job_id = $1 AND owner = $2",
        &[&job_id, &owner, &expires_at, &now],
    )?;
    if updated == 0 {
        return Err(StoreError::LeaseNotHeld);
    }
    Ok(expires_at)
}

/// Startup recovery: reclaim leases whose owner died, incrementing attempts so
/// a permanently crashing sink eventually reaches `dead`.
pub fn recover_expired_leases(
    client: &mut Client,
    now: DateTime<Utc>,
) -> Result<usize, StoreError> {
    let mut transaction = client.transaction()?;
    let rows = transaction.query(
        "SELECT j.id, j.attempts, j.max_attempts
           FROM engram_ingest.jobs j
           JOIN engram_ingest.leases l ON l.job_id = j.id
          WHERE j.state = 'leased' AND l.expires_at <= $1
          FOR UPDATE OF j SKIP LOCKED",
        &[&now],
    )?;
    let mut count = 0usize;
    for row in rows {
        let job_id: i64 = row.get(0);
        let attempts: i32 = row.get(1);
        let max_attempts: i32 = row.get(2);
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
        let next_attempts = attempts + 1;
        let dead = next_attempts >= max_attempts;
        let run_after = now + Duration::milliseconds(RETRY_BASE_MS * (1_i64 << attempts));
        transaction.execute(
            "UPDATE engram_ingest.jobs
                SET state = $2, attempts = $3, run_after = $4,
                    last_error = 'lease expired', updated_at = $5
              WHERE id = $1",
            &[
                &job_id,
                &(if dead { "dead" } else { "pending" }),
                &next_attempts,
                &run_after,
                &now,
            ],
        )?;
        count += 1;
    }
    transaction.commit()?;
    Ok(count)
}

pub fn fail_job(
    client: &mut Client,
    job_id: i64,
    owner: &str,
    now: DateTime<Utc>,
    error: &str,
) -> Result<(), StoreError> {
    let mut transaction = client.transaction()?;
    let row = transaction.query_opt(
        "SELECT j.attempts, j.max_attempts
           FROM engram_ingest.jobs j
           JOIN engram_ingest.leases l ON l.job_id = j.id
          WHERE j.id = $1 AND j.state = 'leased' AND l.owner = $2
          FOR UPDATE",
        &[&job_id, &owner],
    )?;
    let Some(row) = row else {
        transaction.rollback()?;
        return Err(StoreError::LeaseNotHeld);
    };
    let attempts: i32 = row.get(0);
    let max_attempts: i32 = row.get(1);
    let next_attempts = attempts + 1;
    let dead = next_attempts >= max_attempts;
    let run_after = now + Duration::milliseconds(RETRY_BASE_MS * (1_i64 << attempts));
    transaction.execute(
        "UPDATE engram_ingest.jobs
            SET state = $2, attempts = $3, run_after = $4, last_error = $5, updated_at = $6
          WHERE id = $1",
        &[
            &job_id,
            &(if dead { "dead" } else { "pending" }),
            &next_attempts,
            &run_after,
            &error,
            &now,
        ],
    )?;
    transaction.execute(
        "DELETE FROM engram_ingest.leases WHERE job_id = $1 AND owner = $2",
        &[&job_id, &owner],
    )?;
    transaction.commit()?;
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
        ] {
            assert!(
                LEDGER_MIGRATION.contains(&format!("engram_ingest.{table}")),
                "missing {table}"
            );
        }
    }
}
