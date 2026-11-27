-- Native transcript ingestion state (issue #245).
--
-- One source row per transcript root; transcript files reuse the docs ledger
-- (documents + chunks + jobs) and the worker supervisor. This migration adds
-- the transcript source kind, the removal tombstone reason, and the durable
-- per-transcript ack watermark.
--
-- Rollback (after stopping writers and backing up):
--   DELETE FROM engram_ingest.sources WHERE source_kind = 'transcript';
--   DROP TABLE engram_ingest.transcript_windows;
--   DROP TABLE engram_ingest.transcript_watermarks;
--   ALTER TABLE engram_ingest.tombstones DROP CONSTRAINT tombstones_reason_check;
--   ALTER TABLE engram_ingest.tombstones ADD CONSTRAINT tombstones_reason_check
--     CHECK (reason IN ('file_removed','chunk_removed','issue_removed'));
--   ALTER TABLE engram_ingest.sources DROP CONSTRAINT sources_source_kind_check;
--   ALTER TABLE engram_ingest.sources ADD CONSTRAINT sources_source_kind_check
--     CHECK (source_kind IN ('docs','github_issue','github_pr','jira_issue',
--                            'github_discussion','github_board'));
--   DELETE FROM engram_ingest.schema_migrations WHERE version = 7;

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
        'github_board',
        'transcript'
    ));

ALTER TABLE engram_ingest.tombstones
    DROP CONSTRAINT IF EXISTS tombstones_reason_check;

ALTER TABLE engram_ingest.tombstones
    ADD CONSTRAINT tombstones_reason_check
    CHECK (reason IN (
        'file_removed',
        'chunk_removed',
        'issue_removed',
        'transcript_removed'
    ));

CREATE TABLE IF NOT EXISTS engram_ingest.transcript_watermarks (
    source_id      bigint NOT NULL REFERENCES engram_ingest.sources(id) ON DELETE CASCADE,
    relative_path  text NOT NULL,
    transcript_id  text NOT NULL,
    message_count  integer NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    prefix_sha256  bytea CHECK (prefix_sha256 IS NULL OR octet_length(prefix_sha256) = 32),
    observed_message_count integer NOT NULL DEFAULT 0 CHECK (observed_message_count >= 0),
    observed_prefix_sha256 bytea CHECK (
        observed_prefix_sha256 IS NULL OR octet_length(observed_prefix_sha256) = 32
    ),
    last_processed timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, relative_path)
);

CREATE INDEX IF NOT EXISTS transcript_watermarks_transcript_idx
    ON engram_ingest.transcript_watermarks (source_id, transcript_id);

-- Durable window -> transcript ownership. The watermark query joins this
-- table instead of trusting the Hindsight payload, which stays byte-compatible
-- with the Python retain request. `status` distinguishes a retained window from
-- one withheld for pending review; invalidation state makes a live PATCH
-- retryable and exactly-once after a successful acknowledgement.
CREATE TABLE IF NOT EXISTS engram_ingest.transcript_windows (
    source_id             bigint NOT NULL REFERENCES engram_ingest.sources(id) ON DELETE CASCADE,
    hindsight_document_id text NOT NULL,
    transcript_id         text NOT NULL,
    relative_path         text NOT NULL DEFAULT '',
    message_count         integer NOT NULL CHECK (message_count >= 0),
    signal_index          integer NOT NULL DEFAULT 0 CHECK (signal_index >= 0),
    status                text NOT NULL DEFAULT 'retained'
                              CHECK (status IN ('retained', 'queued')),
    invalidate_memory_id  text,
    invalidate_reason     text,
    invalidation_state    text NOT NULL DEFAULT 'none'
                              CHECK (invalidation_state IN ('none', 'pending', 'done')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, hindsight_document_id)
);

CREATE INDEX IF NOT EXISTS transcript_windows_watermark_idx
    ON engram_ingest.transcript_windows (source_id, transcript_id, message_count);

CREATE INDEX IF NOT EXISTS transcript_windows_path_idx
    ON engram_ingest.transcript_windows (source_id, relative_path);
