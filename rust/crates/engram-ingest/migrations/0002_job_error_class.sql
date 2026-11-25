-- Sink error classification (issue #214, Hindsight sink slice).
-- The migration runner inserts schema_migrations version 2.

ALTER TABLE engram_ingest.jobs
    ADD COLUMN IF NOT EXISTS last_error_class text;
