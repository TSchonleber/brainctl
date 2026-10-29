-- Migration 085: fix memories_fts update-trigger firing order
--
-- SQLite fires same-event triggers newest-first. Migrations 052/083 created
-- memories_fts_update_delete before memories_fts_update_insert, so the insert
-- leg ran first. For an UPDATE that leaves content unchanged (category-only,
-- tags-only, same-text rewrite) the late 'delete' then removed the tokens the
-- insert had just added, and the memory vanished from search.
--
-- Recreate the pair with the insert leg first so the delete leg fires first,
-- then rebuild to repair rows already dropped from the index.
--
-- Rollback:
--   re-run migration 083
--   DELETE FROM schema_version WHERE version = 85;
--
-- IDEMPOTENT.

DROP TRIGGER IF EXISTS memories_fts_update_delete;
DROP TRIGGER IF EXISTS memories_fts_update_insert;

CREATE TRIGGER memories_fts_update_insert AFTER UPDATE OF content, category, tags, indexed, retired_at ON memories WHEN new.indexed = 1 AND new.retired_at IS NULL BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags)
    VALUES (new.id, new.content, new.category, new.tags);
END;

CREATE TRIGGER memories_fts_update_delete AFTER UPDATE OF content, category, tags, indexed, retired_at ON memories WHEN old.indexed = 1 BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES ('delete', old.id, old.content, old.category, old.tags);
END;

INSERT INTO memories_fts(memories_fts) VALUES('rebuild');

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (85, 'fix memories_fts update-trigger firing order',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
