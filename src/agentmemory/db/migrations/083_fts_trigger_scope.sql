-- Migration 083: scope memories_fts update triggers (issue #152)
--
-- The unscoped AFTER UPDATE triggers fired on every metadata write, so each
-- memory_search (via _retrieval_practice_boost) eroded the external-content
-- FTS5 index. Scope them to the indexed columns. The one-time rebuild
-- repairs indexes already corrupted in the field.
--
-- Rollback:
--   restore the unscoped triggers from migration 052
--   DELETE FROM schema_version WHERE version = 83;
--
-- IDEMPOTENT.

DROP TRIGGER IF EXISTS memories_fts_update_delete;
DROP TRIGGER IF EXISTS memories_fts_update_insert;

CREATE TRIGGER memories_fts_update_delete AFTER UPDATE OF content, category, tags, indexed, retired_at ON memories WHEN old.indexed = 1 BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES ('delete', old.id, old.content, old.category, old.tags);
END;

CREATE TRIGGER memories_fts_update_insert AFTER UPDATE OF content, category, tags, indexed, retired_at ON memories WHEN new.indexed = 1 AND new.retired_at IS NULL BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags)
    VALUES (new.id, new.content, new.category, new.tags);
END;

INSERT INTO memories_fts(memories_fts) VALUES('rebuild');

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (83, 'scope memories_fts update triggers (issue #152)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
