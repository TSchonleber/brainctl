-- Migration 045: Memory RBAC — visibility column + read_acl
-- (RENUMBERED from original 017 in v2.2.0 — see migrate.py header
-- comment "DUPLICATE-VERSION HISTORY" for context.)
-- Adds two-layer access control to memories:
--   visibility: 'public' | 'project' | 'agent' | 'restricted'  (DEFAULT public)
--   read_acl:   JSON array of agent_ids allowed to read (nullable, for 'agent'/'restricted')
--
-- Non-destructive: existing memories default to 'public'.
--
-- IDEMPOTENT:
--  * `ADD COLUMN` rows are guarded at runtime by migrate.py
--    (`_apply_sql` strips out duplicate-column failures so re-applying
--    is safe). The raw SQL stays as ALTER TABLE because SQLite has no
--    native `ADD COLUMN IF NOT EXISTS` syntax.
--  * Triggers and indexes use IF NOT EXISTS.

ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public';
ALTER TABLE memories ADD COLUMN read_acl TEXT;  -- nullable JSON array e.g. '["agent-a","agent-b"]'

-- Validation trigger: reject invalid visibility values on INSERT
CREATE TRIGGER IF NOT EXISTS memories_visibility_check_insert
BEFORE INSERT ON memories
WHEN NEW.visibility NOT IN ('public', 'project', 'agent', 'restricted')
BEGIN
    SELECT RAISE(ABORT, 'memories.visibility must be one of: public, project, agent, restricted');
END;

-- Validation trigger: reject invalid visibility values on UPDATE
CREATE TRIGGER IF NOT EXISTS memories_visibility_check_update
BEFORE UPDATE OF visibility ON memories
WHEN NEW.visibility NOT IN ('public', 'project', 'agent', 'restricted')
BEGIN
    SELECT RAISE(ABORT, 'memories.visibility must be one of: public, project, agent, restricted');
END;

-- Index for fast visibility filtering
CREATE INDEX IF NOT EXISTS idx_memories_visibility ON memories(visibility);

-- Backfill: ensure all existing memories are public (should already be default, but be explicit)
UPDATE memories SET visibility = 'public' WHERE visibility IS NULL OR visibility = '';

INSERT INTO schema_version (version, description)
VALUES (45, 'Memory RBAC — visibility + read_acl on memories (renumbered from 017)');
