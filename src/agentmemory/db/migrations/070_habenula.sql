-- Migration 070: lateral habenula subsystem — Phase 1 schema
--
-- The "anti-reward" / negative-RPE source. Pairs antisymmetrically
-- with LC (LC = positive surprise → NE; Hb = negative surprise /
-- reward omission / aversion → DA suppression in Phase 3).
--
-- Phase 1 is inspection-only / additive: schema + read+CRUD tools.
-- Does NOT yet damp bg_modulators.tonic_da. That's Phase 3.
--
-- Four invariants encoded:
--   1. Negative-RPE coding: signed_pe always <= 0.
--   2. Reward omission distinct from punishment (event_kind).
--   3. Tonic vs phasic separation.
--   4. DA-suppression effect proportional to integrated activity
--      (Phase 3 will use EWMA; Phase 1 just records events).
--
-- Rollback:
--   DROP TABLE IF EXISTS habenula_state;
--   DROP TABLE IF EXISTS habenula_firings;
--   DROP TABLE IF EXISTS habenula_triggers;
--   DELETE FROM schema_version WHERE version = 70;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS habenula_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    event_kind TEXT NOT NULL CHECK(event_kind IN ('omission', 'aversive', 'repeated_failure', 'other')),
    default_pe REAL NOT NULL DEFAULT -0.1 CHECK(default_pe <= 0.0),
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_hb_triggers_kind ON habenula_triggers(event_kind);

CREATE TABLE IF NOT EXISTS habenula_firings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    trigger_id INTEGER,
    event_kind TEXT NOT NULL CHECK(event_kind IN ('omission', 'aversive', 'repeated_failure', 'other')),
    signed_pe REAL NOT NULL CHECK(signed_pe <= 0.0),
    context_hash TEXT,
    source_event_id INTEGER,
    notes TEXT,
    FOREIGN KEY (trigger_id) REFERENCES habenula_triggers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_hb_firings_recent ON habenula_firings(fired_at);
CREATE INDEX IF NOT EXISTS idx_hb_firings_agent ON habenula_firings(agent_id, fired_at);
CREATE INDEX IF NOT EXISTS idx_hb_firings_kind ON habenula_firings(event_kind, fired_at);

CREATE TABLE IF NOT EXISTS habenula_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tonic_activity REAL NOT NULL DEFAULT 0.0,
    phasic_burst REAL NOT NULL DEFAULT 0.0,
    rolling_disappointment_24h INTEGER NOT NULL DEFAULT 0,
    last_firing_at TEXT,
    suggested_da_damp REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO habenula_state (id) VALUES (1);

INSERT OR IGNORE INTO habenula_triggers (name, event_kind, default_pe, description) VALUES
    ('reward_omission', 'omission', -0.15, 'expected positive outcome did not arrive'),
    ('retrieval_failure', 'omission', -0.10, 'memory_search returned no useful candidates'),
    ('repeated_low_utility', 'repeated_failure', -0.20, 'same query pattern failed 3+ times in 24h'),
    ('aversive_valence', 'aversive', -0.30, 'amygdala flagged content with strong negative valence'),
    ('task_abandoned', 'repeated_failure', -0.25, 'agent abandoned a task after failure cascade');

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (70, 'habenula Phase 1: 3 tables (triggers, firings, state) + 5 seed trigger classes',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
