-- Native issue ingestion state (issue #243).
--
-- Issues reuse the docs ledger: one source row per (provider, repo, item kind)
-- or Jira stream, synthetic relative paths per issue, and the existing job
-- cipher. This migration adds the source kind, the issue-specific tombstone
-- reason, and the per-stream poll cursor.

ALTER TABLE engram_ingest.sources
    ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'docs';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'engram_ingest' AND t.relname = 'sources'
           AND c.conname = 'sources_source_kind_check'
    ) THEN
        ALTER TABLE engram_ingest.sources
            ADD CONSTRAINT sources_source_kind_check
            CHECK (source_kind IN ('docs', 'github_issue', 'github_pr', 'jira_issue'));
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'engram_ingest' AND t.relname = 'tombstones'
           AND c.conname = 'tombstones_reason_check'
    ) THEN
        ALTER TABLE engram_ingest.tombstones
            DROP CONSTRAINT tombstones_reason_check;
    END IF;

    ALTER TABLE engram_ingest.tombstones
        ADD CONSTRAINT tombstones_reason_check
        CHECK (reason IN ('file_removed', 'chunk_removed', 'issue_removed'));
END $$;

CREATE TABLE IF NOT EXISTS engram_ingest.issue_cursors (
    source_id      bigint PRIMARY KEY REFERENCES engram_ingest.sources(id) ON DELETE CASCADE,
    etag           text,
    fingerprint    text,
    last_updated   text,
    last_polled_at timestamptz,
    item_count     integer NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    page_count     integer NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    last_error     text,
    status         text NOT NULL DEFAULT 'idle'
                        CHECK (status IN ('idle', 'applying', 'ok', 'not_modified', 'error')),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
