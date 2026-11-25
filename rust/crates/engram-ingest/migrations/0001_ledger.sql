-- Native Engram ingestion ledger (issue #214, slice 2).
--
-- One schema for every project; project/source identity lives in rows. The
-- Python CocoIndex LMDB memoization state is intentionally not ported.

CREATE SCHEMA IF NOT EXISTS engram_ingest;

CREATE TABLE IF NOT EXISTS engram_ingest.schema_migrations (
    version     bigint PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS engram_ingest.sources (
    id               bigserial PRIMARY KEY,
    project          text NOT NULL,
    tag              text NOT NULL,
    root             text NOT NULL,
    docs_include     text[] NOT NULL DEFAULT '{}',
    docs_exclude     text[] NOT NULL DEFAULT '{}',
    document_format  text NOT NULL DEFAULT 'markdown',
    enabled          boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project, tag, root)
);

CREATE TABLE IF NOT EXISTS engram_ingest.documents (
    id              bigserial PRIMARY KEY,
    source_id       bigint NOT NULL REFERENCES engram_ingest.sources(id) ON DELETE CASCADE,
    relative_path   text NOT NULL,
    content_sha256  bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    size_bytes      bigint NOT NULL CHECK (size_bytes >= 0),
    layout_version  integer NOT NULL,
    state           text NOT NULL DEFAULT 'active'
                        CHECK (state IN ('active', 'tombstoned')),
    tombstoned_at   timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT documents_source_path_key UNIQUE (source_id, relative_path)
);

CREATE INDEX IF NOT EXISTS documents_source_state_idx
    ON engram_ingest.documents (source_id, state);

CREATE TABLE IF NOT EXISTS engram_ingest.chunks (
    id                     bigserial PRIMARY KEY,
    source_id              bigint NOT NULL REFERENCES engram_ingest.sources(id) ON DELETE CASCADE,
    document_id            bigint NOT NULL REFERENCES engram_ingest.documents(id) ON DELETE CASCADE,
    hindsight_document_id  text NOT NULL,
    chunk_key              text NOT NULL DEFAULT '',
    content_sha256         bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    ordinal                integer NOT NULL,
    generation             integer NOT NULL DEFAULT 1 CHECK (generation > 0),
    state                  text NOT NULL DEFAULT 'active'
                               CHECK (state IN ('active', 'tombstoned')),
    tombstoned_at          timestamptz,
    CONSTRAINT chunks_source_document_key UNIQUE (source_id, hindsight_document_id),
    CONSTRAINT chunks_document_chunk_key UNIQUE (document_id, chunk_key)
);

CREATE INDEX IF NOT EXISTS chunks_state_idx
    ON engram_ingest.chunks (state);

CREATE TABLE IF NOT EXISTS engram_ingest.jobs (
    id                     bigserial PRIMARY KEY,
    source_id              bigint NOT NULL REFERENCES engram_ingest.sources(id) ON DELETE CASCADE,
    kind                   text NOT NULL CHECK (kind IN ('retain', 'delete')),
    hindsight_document_id  text NOT NULL,
    dedupe_key             text NOT NULL,
    generation             integer,
    payload                jsonb NOT NULL DEFAULT '{}'::jsonb,
    state                  text NOT NULL DEFAULT 'pending'
                               CHECK (state IN ('pending', 'leased', 'succeeded', 'dead')),
    attempts               integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts           integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    run_after              timestamptz NOT NULL DEFAULT now(),
    last_sink_operation    text,
    last_error             text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_source_dedupe_key UNIQUE (source_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx
    ON engram_ingest.jobs (state, run_after, id);

-- Sink order per document is serialized: at most one leased job may exist for
-- a given (source, document id), so an older delete cannot race a newer retain.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_leased_per_document_idx
    ON engram_ingest.jobs (source_id, hindsight_document_id)
    WHERE state = 'leased';

CREATE TABLE IF NOT EXISTS engram_ingest.leases (
    id            bigserial PRIMARY KEY,
    job_id        bigint NOT NULL UNIQUE REFERENCES engram_ingest.jobs(id) ON DELETE CASCADE,
    owner         text NOT NULL,
    expires_at    timestamptz NOT NULL,
    heartbeat_at  timestamptz NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS leases_expiry_idx
    ON engram_ingest.leases (expires_at);

CREATE TABLE IF NOT EXISTS engram_ingest.tombstones (
    id                     bigserial PRIMARY KEY,
    source_id              bigint NOT NULL REFERENCES engram_ingest.sources(id) ON DELETE CASCADE,
    document_id            bigint REFERENCES engram_ingest.documents(id) ON DELETE SET NULL,
    hindsight_document_id  text NOT NULL,
    relative_path          text NOT NULL,
    reason                 text NOT NULL CHECK (reason IN ('file_removed', 'chunk_removed')),
    generation             integer NOT NULL DEFAULT 1 CHECK (generation > 0),
    sink_deleted_at        timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tombstones_generation_key
        UNIQUE (source_id, hindsight_document_id, reason, generation)
);

CREATE INDEX IF NOT EXISTS tombstones_source_idx
    ON engram_ingest.tombstones (source_id, created_at);

INSERT INTO engram_ingest.schema_migrations (version)
VALUES (1)
ON CONFLICT (version) DO NOTHING;
