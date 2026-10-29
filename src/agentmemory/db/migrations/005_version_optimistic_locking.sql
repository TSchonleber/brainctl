-- Migration 005: Optimistic Locking — version column for memories (CAS)
-- Author: Kernel (Backend Engineer)
-- Date: 2026-03-28
-- Purpose: Enable compare-and-swap concurrent updates to memories.
--          Each write increments version; callers must supply the expected
--          version or the UPDATE silently rejects (0 rows affected).
-- Schema version: 4 -> 5

ALTER TABLE memories ADD COLUMN version INTEGER NOT NULL DEFAULT 1;

-- Index to speed up CAS lookups (id + version predicate)
CREATE INDEX idx_memories_id_version ON memories(id, version) WHERE retired_at IS NULL;

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (5, datetime('now'), 'version column on memories for optimistic locking (CAS)');

PRAGMA user_version = 5;
