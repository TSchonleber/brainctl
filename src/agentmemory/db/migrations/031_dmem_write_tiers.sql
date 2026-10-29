-- Migration 031: D-MEM RPE routing — three-tier write gate (issue #31)
-- Adds write_tier (skip/construct/full), indexed flag, and promoted_at timestamp
-- to memories table, plus memory_stats for long-term utility estimation.
-- Updates FTS5 triggers to only index when indexed = 1.

ALTER TABLE memories ADD COLUMN write_tier TEXT NOT NULL DEFAULT 'full'
    CHECK(write_tier IN ('skip', 'construct', 'full'));
ALTER TABLE memories ADD COLUMN indexed INTEGER NOT NULL DEFAULT 1;
ALTER TABLE memories ADD COLUMN promoted_at TEXT DEFAULT NULL;

-- memory_stats: per-(agent, category, scope) average recall rate for long-term utility
CREATE TABLE IF NOT EXISTS memory_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    category TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    avg_recall_rate REAL NOT NULL DEFAULT 0.5,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(agent_id, category, scope)
);
CREATE INDEX IF NOT EXISTS idx_memory_stats_agent ON memory_stats(agent_id, category, scope);

-- Update FTS5 triggers to skip unindexed memories.
-- Insert: only index when indexed = 1
DROP TRIGGER IF EXISTS memories_fts_insert;
CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories WHEN new.indexed = 1 BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags)
    VALUES (new.id, new.content, new.category, new.tags);
END;

-- Update: remove old FTS entry if it was indexed; add new if now indexed.
-- Split into two triggers to handle 0→1 promotion correctly.
DROP TRIGGER IF EXISTS memories_fts_update;
DROP TRIGGER IF EXISTS memories_fts_update_delete;
DROP TRIGGER IF EXISTS memories_fts_update_insert;

CREATE TRIGGER memories_fts_update_delete AFTER UPDATE ON memories WHEN old.indexed = 1 BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES ('delete', old.id, old.content, old.category, old.tags);
END;

CREATE TRIGGER memories_fts_update_insert AFTER UPDATE ON memories WHEN new.indexed = 1 BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags)
    VALUES (new.id, new.content, new.category, new.tags);
END;
