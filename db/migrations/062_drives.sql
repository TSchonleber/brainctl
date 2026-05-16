-- Migration 062: Drives / hypothalamus — homeostatic set-points
-- Named needs (consolidation_debt, staleness, belief_coverage, pii_pressure,
-- entity_freshness) with set-points; current levels produce drive magnitudes
-- that BIAS — but never directly override — downstream gating.

CREATE TABLE IF NOT EXISTS drive_definitions (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    set_point REAL NOT NULL,
    hard_threshold REAL,
    sample_query TEXT NOT NULL,
    recommended_mode TEXT,
    is_safety_drive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS drive_current_state (
    name TEXT PRIMARY KEY REFERENCES drive_definitions(name) ON DELETE CASCADE,
    current_level REAL NOT NULL DEFAULT 0.0,
    error REAL NOT NULL DEFAULT 0.0,
    magnitude REAL NOT NULL DEFAULT 0.0,
    in_hard_state INTEGER NOT NULL DEFAULT 0,
    sampled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS drive_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    current_level REAL NOT NULL,
    error REAL NOT NULL,
    magnitude REAL NOT NULL,
    sampled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
CREATE INDEX IF NOT EXISTS idx_drive_history_name_time ON drive_history(name, sampled_at DESC);

-- Seed the five canonical brainctl drives.
INSERT OR IGNORE INTO drive_definitions (name, description, set_point, hard_threshold, sample_query, recommended_mode, is_safety_drive) VALUES
  ('consolidation_debt', 'Memories with low replay_priority and stale last_recalled_at', 0.0, NULL, 'SELECT 0.0', 'incident', 0),
  ('staleness',          'Time since last event in active project scope (hours)', 6.0, NULL, 'SELECT 0.0', 'wake_exploratory', 0),
  ('belief_coverage',    'Fraction of entities with NULL compiled_truth', 0.7, NULL, 'SELECT 0.0', NULL, 0),
  ('pii_pressure',       'Unprocessed PII recency queue depth', 0.0, 10.0, 'SELECT 0.0', NULL, 1),
  ('entity_freshness',   'Fraction of entities without recent entity_observe (14d)', 0.6, NULL, 'SELECT 0.0', 'focused_work', 0);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (62, 'Drives/hypothalamus: 5 homeostatic set-points + 1 PAG-style safety drive (pii_pressure)', strftime('%Y-%m-%dT%H:%M:%S','now'));
