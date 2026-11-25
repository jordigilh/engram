-- Bank identity for sources and jobs (issue #214, worker-loop slice).
--
-- source bank_id is explicit: the migration runner refuses to guess one for an
-- existing row. The unversioned guard in store::migrate() backfills jobs and
-- enforces NOT NULL once every source has a bank. The migration runner inserts
-- schema_migrations version 3.

-- SET NOT NULL in the guard needs ACCESS EXCLUSIVE, and this DDL must not race
-- a live reconciliation writer.
LOCK TABLE engram_ingest.sources, engram_ingest.jobs IN ACCESS EXCLUSIVE MODE;

ALTER TABLE engram_ingest.sources
    ADD COLUMN IF NOT EXISTS bank_id text;

ALTER TABLE engram_ingest.jobs
    ADD COLUMN IF NOT EXISTS bank_id text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'engram_ingest' AND t.relname = 'sources'
           AND c.conname = 'sources_bank_id_format'
    ) THEN
        ALTER TABLE engram_ingest.sources
            ADD CONSTRAINT sources_bank_id_format
            CHECK (bank_id IS NULL OR (bank_id ~ '^[A-Za-z0-9._-]{1,128}$'
                                       AND bank_id <> '.' AND bank_id <> '..'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'engram_ingest' AND t.relname = 'jobs'
           AND c.conname = 'jobs_bank_id_format'
    ) THEN
        ALTER TABLE engram_ingest.jobs
            ADD CONSTRAINT jobs_bank_id_format
            CHECK (bank_id IS NULL OR (bank_id ~ '^[A-Za-z0-9._-]{1,128}$'
                                       AND bank_id <> '.' AND bank_id <> '..'));
    END IF;
END $$;

UPDATE engram_ingest.jobs j
   SET bank_id = s.bank_id
  FROM engram_ingest.sources s
 WHERE j.source_id = s.id
   AND j.bank_id IS NULL
   AND s.bank_id IS NOT NULL;

-- The claim filters bank_id + state and orders by id; a partial index keeps the
-- due-set scan and ordering index-served.
CREATE INDEX IF NOT EXISTS jobs_bank_pending_idx
    ON engram_ingest.jobs (bank_id, id)
    WHERE state = 'pending';
