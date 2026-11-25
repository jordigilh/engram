-- Serialize sink operations per bank document, not only per source (issue #214).
--
-- Two sources in one bank can share a tag (and therefore a Hindsight document
-- id). The original per-source lease index let their retain/delete jobs run
-- concurrently and reorder. The migration runner inserts version 4.
--
-- Live leases are never revoked: upgrading requires ingest workers to be
-- stopped (or drained) first. Only expired surplus leases are requeued; any
-- remaining live duplicate aborts the migration with an actionable message so a
-- stale in-flight Hindsight write cannot overtake a requeued job.

WITH ranked AS (
    SELECT j.id,
           row_number() OVER (
               PARTITION BY j.bank_id, j.hindsight_document_id
               ORDER BY j.id
           ) AS rank
      FROM engram_ingest.jobs j
      JOIN engram_ingest.leases l ON l.job_id = j.id
     WHERE j.state = 'leased'
       AND j.bank_id IS NOT NULL
       AND l.expires_at <= now()
),
deleted AS (
    DELETE FROM engram_ingest.leases l
     USING ranked r
     WHERE l.job_id = r.id AND r.rank > 1
    RETURNING l.job_id
)
UPDATE engram_ingest.jobs j
   SET state = 'pending',
       run_after = now(),
       last_error = 'duplicate lease requeued by bank-document migration',
       last_error_class = 'transient',
       updated_at = now()
  FROM deleted d
 WHERE j.id = d.job_id;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM engram_ingest.jobs j
          JOIN engram_ingest.leases l ON l.job_id = j.id
         WHERE j.state = 'leased'
           AND j.bank_id IS NOT NULL
         GROUP BY j.bank_id, j.hindsight_document_id
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION
            'live duplicate leases exist for one bank document; stop ingest workers, let leases expire or drain them, then rerun migrate()';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM engram_ingest.jobs WHERE bank_id IS NULL
    ) THEN
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_leased_per_bank_document_idx
            ON engram_ingest.jobs (bank_id, hindsight_document_id)
            WHERE state = 'leased';
        DROP INDEX IF EXISTS engram_ingest.jobs_one_leased_per_document_idx;
    END IF;
END $$;
