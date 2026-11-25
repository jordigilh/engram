-- Serialize sink operations per bank document, not only per source (issue #214).
--
-- Two sources in one bank can share a tag (and therefore a Hindsight document
-- id). The original per-source lease index let their retain/delete jobs run
-- concurrently and reorder. The migration runner inserts version 4.
--
-- On the v2 upgrade path bank_id is still NULL until the unversioned guard
-- backfills it. Only dedupe rows with a real bank here; the guard repeats the
-- dedupe and builds the index after the backfill.

DROP INDEX IF EXISTS engram_ingest.jobs_one_leased_per_document_idx;

WITH ranked AS (
    SELECT j.id,
           row_number() OVER (
               PARTITION BY j.bank_id, j.hindsight_document_id
               ORDER BY j.id
           ) AS rank
      FROM engram_ingest.jobs j
     WHERE j.state = 'leased'
       AND j.bank_id IS NOT NULL
)
DELETE FROM engram_ingest.leases l
 USING ranked r
 WHERE l.job_id = r.id AND r.rank > 1;

WITH ranked AS (
    SELECT j.id,
           row_number() OVER (
               PARTITION BY j.bank_id, j.hindsight_document_id
               ORDER BY j.id
           ) AS rank
      FROM engram_ingest.jobs j
     WHERE j.state = 'leased'
       AND j.bank_id IS NOT NULL
)
UPDATE engram_ingest.jobs j
   SET state = 'pending',
       run_after = now(),
       last_error = 'duplicate lease requeued by bank-document migration',
       last_error_class = 'transient',
       updated_at = now()
  FROM ranked r
 WHERE j.id = r.id AND r.rank > 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM engram_ingest.jobs WHERE bank_id IS NULL
    ) THEN
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_leased_per_bank_document_idx
            ON engram_ingest.jobs (bank_id, hindsight_document_id)
            WHERE state = 'leased';
    END IF;
END $$;
