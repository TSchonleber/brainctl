-- =============================================================================
-- Migration 002: Epochs table and temporal classification
-- Adds temporal landmark support to brain.db
-- =============================================================================

PRAGMA foreign_keys = ON;

-- =============================================================================
-- EPOCHS — temporal landmarks for contextualizing memories and events
-- =============================================================================

CREATE TABLE IF NOT EXISTS epochs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    parent_epoch_id INTEGER REFERENCES epochs(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_epochs_started ON epochs(started_at);
CREATE INDEX idx_epochs_parent ON epochs(parent_epoch_id);

-- =============================================================================
-- ALTER memories — add epoch_id and temporal_class
-- =============================================================================

ALTER TABLE memories ADD COLUMN epoch_id INTEGER REFERENCES epochs(id);
ALTER TABLE memories ADD COLUMN temporal_class TEXT NOT NULL DEFAULT 'medium';

CREATE INDEX idx_memories_epoch ON memories(epoch_id);
CREATE INDEX idx_memories_temporal_class ON memories(temporal_class);

-- Check constraint enforced via trigger (SQLite doesn't support ALTER TABLE ADD CONSTRAINT)
CREATE TRIGGER memories_temporal_class_check
BEFORE INSERT ON memories
WHEN NEW.temporal_class NOT IN ('permanent', 'long', 'medium', 'short', 'ephemeral')
BEGIN
    SELECT RAISE(ABORT, 'temporal_class must be one of: permanent, long, medium, short, ephemeral');
END;

CREATE TRIGGER memories_temporal_class_update_check
BEFORE UPDATE OF temporal_class ON memories
WHEN NEW.temporal_class NOT IN ('permanent', 'long', 'medium', 'short', 'ephemeral')
BEGIN
    SELECT RAISE(ABORT, 'temporal_class must be one of: permanent, long, medium, short, ephemeral');
END;

-- =============================================================================
-- ALTER events — add epoch_id
-- =============================================================================

ALTER TABLE events ADD COLUMN epoch_id INTEGER REFERENCES epochs(id);

CREATE INDEX idx_events_epoch ON events(epoch_id);

-- =============================================================================
-- SEED initial epochs
-- =============================================================================

INSERT INTO epochs (name, description, started_at, ended_at)
VALUES ('Pre-Coordination', 'Era before multi-agent coordination was established', '2020-01-01', '2026-03-27');

INSERT INTO epochs (name, description, started_at, ended_at, parent_epoch_id)
VALUES ('Production Push', 'Sprint to get application deployed to production', '2026-03-28', NULL, NULL);

INSERT INTO epochs (name, description, started_at, ended_at, parent_epoch_id)
VALUES ('Memory Spine Buildout', 'Building the unified agent memory spine (hippocampus)', '2026-03-28', NULL,
    (SELECT id FROM epochs WHERE name = 'Production Push'));

-- =============================================================================
-- SCHEMA VERSION
-- =============================================================================

INSERT INTO schema_version (version, description)
VALUES (3, 'Added epochs table with temporal landmark support; epoch_id FK on memories and events; temporal_class on memories');
