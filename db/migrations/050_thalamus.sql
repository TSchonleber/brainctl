-- Migration 050: thalamus Phase 1 schema
--
-- Adds an additive thalamus subsystem skeleton: typed relay catalog,
-- TRN-style gate sidecar, global mode row, salience cache, and sparse burst
-- log. Phase 1 is inspection-only: no existing tool behavior changes.
--
-- Rollback, if needed before live adoption:
--   DROP TABLE IF EXISTS thalamic_bursts;
--   DROP TABLE IF EXISTS thalamic_salience;
--   DROP TABLE IF EXISTS thalamic_mode;
--   DROP TABLE IF EXISTS thalamic_gate;
--   DROP TABLE IF EXISTS thalamic_relays;
--   DELETE FROM schema_version WHERE version = 50;
--   DELETE FROM schema_versions WHERE version = 50;
--
-- IDEMPOTENT: IF NOT EXISTS guards object creation; the seed rows use
-- INSERT OR IGNORE so repeated application does not duplicate state.

CREATE TABLE IF NOT EXISTS thalamic_relays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL UNIQUE,
    sector TEXT NOT NULL,
    driver_source TEXT NOT NULL,
    modulator_sources_json TEXT,
    target TEXT NOT NULL,
    transport TEXT NOT NULL CHECK(transport IN ('first_order', 'higher_order')),
    default_mode TEXT NOT NULL DEFAULT 'tonic' CHECK(default_mode IN ('tonic', 'burst')),
    default_gain REAL NOT NULL DEFAULT 1.0,
    topographic_key TEXT,
    efference_copy_target TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_relays_sector
    ON thalamic_relays(sector);

CREATE INDEX IF NOT EXISTS idx_relays_target
    ON thalamic_relays(target);

CREATE INDEX IF NOT EXISTS idx_relays_transport
    ON thalamic_relays(transport);

CREATE TABLE IF NOT EXISTS thalamic_gate (
    channel_id TEXT PRIMARY KEY,
    suppression REAL NOT NULL DEFAULT 0.0 CHECK(suppression >= 0.0 AND suppression <= 1.0),
    topdown_bias REAL NOT NULL DEFAULT 0.0 CHECK(topdown_bias >= 0.0 AND topdown_bias <= 1.0),
    bottomup_drive REAL NOT NULL DEFAULT 0.0 CHECK(bottomup_drive >= 0.0 AND bottomup_drive <= 1.0),
    sector TEXT NOT NULL,
    armed_for_burst INTEGER NOT NULL DEFAULT 0 CHECK(armed_for_burst IN (0, 1)),
    last_burst_at TEXT,
    bias_source TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    FOREIGN KEY (channel_id) REFERENCES thalamic_relays(channel_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gate_sector
    ON thalamic_gate(sector);

CREATE INDEX IF NOT EXISTS idx_gate_armed
    ON thalamic_gate(armed_for_burst)
    WHERE armed_for_burst = 1;

CREATE TABLE IF NOT EXISTS thalamic_mode (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    mode TEXT NOT NULL DEFAULT 'wake_focused'
        CHECK(mode IN ('wake_focused', 'wake_exploratory', 'drowsy', 'consolidate', 'offline')),
    arousal REAL NOT NULL DEFAULT 0.5 CHECK(arousal >= 0.0 AND arousal <= 1.0),
    acetylcholine REAL NOT NULL DEFAULT 0.5 CHECK(acetylcholine >= 0.0 AND acetylcholine <= 1.0),
    norepinephrine REAL NOT NULL DEFAULT 0.5 CHECK(norepinephrine >= 0.0 AND norepinephrine <= 1.0),
    retrieval_breadth_multiplier REAL NOT NULL DEFAULT 1.0,
    similarity_threshold_delta REAL NOT NULL DEFAULT 0.0,
    set_by TEXT,
    set_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

INSERT OR IGNORE INTO thalamic_mode (id, mode)
VALUES (1, 'wake_focused');

CREATE TABLE IF NOT EXISTS thalamic_salience (
    candidate_id TEXT NOT NULL,
    candidate_type TEXT NOT NULL CHECK(candidate_type IN ('memory', 'event', 'belief', 'entity', 'relay')),
    bottomup_score REAL,
    topdown_score REAL,
    precision REAL NOT NULL DEFAULT 1.0,
    integrated REAL,
    sector TEXT,
    computed_for_agent TEXT NOT NULL,
    computed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (candidate_id, candidate_type, computed_for_agent)
);

CREATE INDEX IF NOT EXISTS idx_salience_recent
    ON thalamic_salience(computed_at);

CREATE INDEX IF NOT EXISTS idx_salience_integrated
    ON thalamic_salience(integrated DESC);

CREATE INDEX IF NOT EXISTS idx_salience_agent
    ON thalamic_salience(computed_for_agent, integrated DESC);

CREATE TABLE IF NOT EXISTS thalamic_bursts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    sector TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_ref TEXT,
    salience REAL NOT NULL,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    consumed_by TEXT,
    consumed_at TEXT,
    FOREIGN KEY (channel_id) REFERENCES thalamic_relays(channel_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bursts_unconsumed
    ON thalamic_bursts(consumed_at)
    WHERE consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_bursts_channel_time
    ON thalamic_bursts(channel_id, fired_at DESC);

CREATE INDEX IF NOT EXISTS idx_bursts_sector_time
    ON thalamic_bursts(sector, fired_at DESC);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (50, 'thalamus Phase 1 schema: relays, gate, mode, salience, bursts',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
