-- Migration 084: rebuild memories_fts on cold-start (issue #151)
--
-- External-content FTS5 (memories_fts, content=memories) starts with an
-- empty inverted index. Fresh installs never seeded it, so `memory add`
-- followed by `search` returned nothing until a manual rebuild. cmd_init
-- now seeds the index for new DBs; this migration repairs DBs already
-- created without a seeded index.
--
-- An empty external-content index is *consistent*, not *corrupt*, so the
-- startup integrity-check never flagged it — the rebuild here is the fix.
--
-- Rollback:
--   DELETE FROM schema_version WHERE version = 84;
--   (the rebuild leaves no schema change to revert)
--
-- IDEMPOTENT.

INSERT INTO memories_fts(memories_fts) VALUES('rebuild');

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (84, 'rebuild memories_fts on cold-start (issue #151)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
