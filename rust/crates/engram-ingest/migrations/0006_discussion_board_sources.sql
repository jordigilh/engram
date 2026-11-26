-- GitHub discussion and project-board source kinds (issue #244).
--
-- The issue cursor table is reused unchanged. This migration only widens the
-- source-kind constraint while keeping the same named constraint the migration
-- guard verifies.

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
