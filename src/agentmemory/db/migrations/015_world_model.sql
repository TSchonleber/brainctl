-- =============================================================================
-- Migration 015 — World Model: Organizational World Model (OWM) tables
-- (World Model — Compressed queryable model of organizational)
-- =============================================================================

-- agent_capabilities: per-agent proficiency scores by capability domain
-- More structured than agent_expertise (which uses raw keyword tokens).
-- Capabilities here are higher-level functional domains derived from event history.
CREATE TABLE IF NOT EXISTS agent_capabilities (
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    capability      TEXT NOT NULL,          -- e.g. "sql_migration", "research", "memory_ops"
    skill_level     REAL NOT NULL DEFAULT 0.5,   -- 0.0-1.0 estimated proficiency
    task_count      INTEGER NOT NULL DEFAULT 0,  -- result events logged in this domain
    avg_events      REAL,                    -- avg events per task burst (proxy for effort)
    block_rate      REAL DEFAULT 0.0,        -- fraction of events that were blocked/errors
    last_active     TEXT,                    -- last event timestamp in this domain
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (agent_id, capability)
);

CREATE INDEX IF NOT EXISTS idx_agent_caps_agent ON agent_capabilities(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_caps_cap ON agent_capabilities(capability);
CREATE INDEX IF NOT EXISTS idx_agent_caps_skill ON agent_capabilities(skill_level DESC);

-- world_model_snapshots: prediction vs actual logging for calibrated forecasting
-- Hermes (or any agent) can log a prediction, then update with actual once resolved.
CREATE TABLE IF NOT EXISTS world_model_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_type    TEXT NOT NULL,          -- 'org_state' | 'prediction' | 'error_log'
    subject_id       TEXT,                   -- agent_id, project name, or task ref
    subject_type     TEXT,                   -- 'agent' | 'project' | 'task'
    predicted_state  TEXT,                   -- JSON: the predicted state
    actual_state     TEXT,                   -- JSON: filled in after resolution
    prediction_error REAL,                   -- scalar distance |predicted - actual| (0.0-1.0)
    author_agent_id  TEXT REFERENCES agents(id),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    resolved_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_wm_snapshots_type ON world_model_snapshots(snapshot_type);
CREATE INDEX IF NOT EXISTS idx_wm_snapshots_subject ON world_model_snapshots(subject_id);
CREATE INDEX IF NOT EXISTS idx_wm_snapshots_unresolved ON world_model_snapshots(resolved_at) WHERE resolved_at IS NULL;

-- Record this migration
INSERT OR IGNORE INTO schema_version (version, description)
VALUES (15, 'World Model — agent_capabilities, world_model_snapshots ');
