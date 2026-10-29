-- =============================================================================
-- Migration 017: Global Workspace — salience_score + gw_broadcast on memories
-- Implements : per-memory salience scoring and broadcast flag.
-- Agents call `brainctl gw listen` to see the current attention spotlight.
-- Depends on: workspace_broadcasts (013), MEB (010)
-- =============================================================================

PRAGMA foreign_keys = ON;

-- Add salience_score: computed during each consolidation pass (trust × confidence × recency proxy)
ALTER TABLE memories ADD COLUMN salience_score REAL NOT NULL DEFAULT 0.0;

-- Add gw_broadcast flag: set to 1 when salience_score > ignition_threshold (typically 0.85)
ALTER TABLE memories ADD COLUMN gw_broadcast INTEGER NOT NULL DEFAULT 0;

-- Index for fast GW listen queries
CREATE INDEX IF NOT EXISTS idx_memories_gw_broadcast ON memories(gw_broadcast) WHERE gw_broadcast = 1;
CREATE INDEX IF NOT EXISTS idx_memories_salience ON memories(salience_score DESC) WHERE retired_at IS NULL;

-- Trigger: when gw_broadcast flips from 0 to 1, emit a high-priority MEB event
CREATE TRIGGER IF NOT EXISTS trg_gw_broadcast_meb
AFTER UPDATE OF gw_broadcast ON memories
WHEN NEW.gw_broadcast = 1 AND OLD.gw_broadcast = 0 AND NEW.retired_at IS NULL
BEGIN
    INSERT INTO memory_events (memory_id, agent_id, operation, category, scope, memory_type, created_at)
    VALUES (
        NEW.id,
        NEW.agent_id,
        'broadcast',
        NEW.category,
        COALESCE(NEW.scope, 'global'),
        COALESCE(NEW.memory_type, 'episodic'),
        strftime('%Y-%m-%dT%H:%M:%S', 'now')
    );
END;

-- Trigger: when gw_broadcast flips from 0 to 1, also insert a workspace_broadcasts entry if not already present
CREATE TRIGGER IF NOT EXISTS trg_gw_broadcast_workspace
AFTER UPDATE OF gw_broadcast ON memories
WHEN NEW.gw_broadcast = 1 AND OLD.gw_broadcast = 0 AND NEW.retired_at IS NULL
BEGIN
    INSERT OR IGNORE INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by)
    SELECT
        NEW.id,
        NEW.agent_id,
        NEW.salience_score,
        substr(NEW.content, 1, 200),
        COALESCE(NEW.scope, 'global'),
        'gw_score'
    WHERE NOT EXISTS (
        SELECT 1 FROM workspace_broadcasts wb WHERE wb.memory_id = NEW.id
          AND wb.broadcast_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-48 hours'))
    );
END;

-- =============================================================================
-- SCHEMA VERSION
-- =============================================================================

INSERT INTO schema_version (version, description)
VALUES (17, 'Global Workspace memory columns — salience_score + gw_broadcast on memories ');
