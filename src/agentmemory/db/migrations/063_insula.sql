-- Migration 063: Insula — interoception / self-state subsystem
-- Maps brainctl's internal state (queue depths, error rates, retrieval
-- latency, write pressure, certainty) into a unified felt-state vector
-- that other subsystems subscribe to. Predicted baseline + deviation —
-- consumers react to PREDICTION ERROR on interoceptive signals.

CREATE TABLE IF NOT EXISTS insula_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_name TEXT NOT NULL,
    raw_value REAL NOT NULL,
    normalized_value REAL NOT NULL CHECK(normalized_value >= 0.0 AND normalized_value <= 1.0),
    baseline_ema REAL,
    deviation REAL,
    source TEXT,
    sampled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
CREATE INDEX IF NOT EXISTS idx_insula_signals_name_time ON insula_signals(signal_name, sampled_at DESC);

-- Singleton aggregate — the current felt state.
CREATE TABLE IF NOT EXISTS insula_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    write_pressure REAL NOT NULL DEFAULT 0.0,
    retrieval_strain REAL NOT NULL DEFAULT 0.0,
    consolidation_debt REAL NOT NULL DEFAULT 0.0,
    embedding_health REAL NOT NULL DEFAULT 1.0,
    attention_load REAL NOT NULL DEFAULT 0.0,
    certainty REAL NOT NULL DEFAULT 0.5,
    felt_state_label TEXT NOT NULL DEFAULT 'calm',
    urgency_score REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
INSERT OR IGNORE INTO insula_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS insula_subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subsystem TEXT NOT NULL,
    signal_name TEXT NOT NULL,
    threshold REAL NOT NULL,
    comparator TEXT NOT NULL DEFAULT 'gt' CHECK(comparator IN ('gt','lt','abs_gt')),
    action_hint TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired_at TEXT,
    UNIQUE(subsystem, signal_name, action_hint)
);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (63, 'Insula: self-state interoception (signals + state + subscribers)', strftime('%Y-%m-%dT%H:%M:%S','now'));
