-- Migration 051: code_ingest_cache
--
-- Content-hash cache for the `brainctl ingest code` pipeline (introduced
-- alongside the optional `brainctl[code]` extra). On re-ingest we compute
-- a SHA256 of each source file's bytes and skip the tree-sitter pass if
-- (path, content_sha, scope) is unchanged since last run. This keeps
-- re-ingests of a 10k-file repo under a second on unchanged trees and
-- honors the "CPU-only, no LLM" design constraint of the code extra.
--
-- Scope is part of the key so the same file can be ingested into
-- `project:foo` and `project:bar` independently without either shadowing
-- the other's cache.
--
-- No triggers. No FK to `scope` (scopes are free-form strings in the rest
-- of the schema). Retired-entity cleanup is out of scope for this cache —
-- entities written by the ingester live under the normal retired_at
-- semantics and the gap scanner (migration 036) already detects orphans.
--
-- IDEMPOTENT: IF NOT EXISTS guards re-application; schema_version insert
-- is OR IGNORE so re-runs don't collide on the PK.

CREATE TABLE IF NOT EXISTS code_ingest_cache (
    file_path         TEXT NOT NULL,
    scope             TEXT NOT NULL DEFAULT 'global',
    content_sha       TEXT NOT NULL,                     -- hex SHA256 of raw bytes
    language          TEXT NOT NULL,                     -- 'python' | 'typescript' | 'go' | future
    entity_count      INTEGER NOT NULL DEFAULT 0,        -- entities written on last pass
    edge_count        INTEGER NOT NULL DEFAULT 0,        -- knowledge_edges written on last pass
    last_ingested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (file_path, scope)
);

CREATE INDEX IF NOT EXISTS idx_code_ingest_cache_scope
    ON code_ingest_cache(scope);

CREATE INDEX IF NOT EXISTS idx_code_ingest_cache_language
    ON code_ingest_cache(language);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (51, 'code_ingest_cache: SHA256 cache for brainctl[code] tree-sitter ingest (2.4.4)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
