-- GitHub discussion and project-board source kinds (issue #244).
--
-- The issue cursor table is reused unchanged. This migration only widens the
-- source-kind constraint while keeping the same named constraint the migration
-- guard verifies.
--
-- Rollback (after stopping writers and backing up):
--   DELETE FROM engram_ingest.sources WHERE source_kind IN ('github_discussion','github_board');
--   ALTER TABLE engram_ingest.sources DROP CONSTRAINT sources_source_kind_check;
--   ALTER TABLE engram_ingest.sources ADD CONSTRAINT sources_source_kind_check
--     CHECK (source_kind IN ('docs','github_issue','github_pr','jira_issue'));
--   DELETE FROM engram_ingest.schema_migrations WHERE version = 6;
-- The guard rejects a recorded version 6 whose constraint lacks the new kinds.

ALTER TABLE engram_ingest.sources
    DROP CONSTRAINT IF EXISTS sources_source_kind_check;

ALTER TABLE engram_ingest.sources
    ADD CONSTRAINT sources_source_kind_check
    CHECK (source_kind IN (
        'docs',
        'github_issue',
        'github_pr',
        'jira_issue',
        'github_discussion',
        'github_board'
    ));
