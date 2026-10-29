-- Migration 067: locus coeruleus subsystem — Phase 1 schema
--
-- Implements Phase 1 of the LC proposal at
-- docs/proposals/locus_coeruleus.md. LC is the global surprise /
-- norepinephrine-readiness broadcaster that sits between prediction-error
-- sources (cerebellum, BG, novelty events) and the downstream
-- bg_modulators.lc_ne dial.
--
-- Phase 1 is inspection-only / additive: schema + read/CRUD tools.
-- No dispatch behavior changes. No writes to bg_modulators.lc_ne happen
-- in this phase; Phase 2 owns shadow wiring and NE broadcast.
--
-- Five biological invariants encoded here:
--   1. Phasic and tonic-shift firing modes are explicit event classes.
--   2. LC state is a single broadcast row, not per-agent private state.
--   3. Trigger taxonomy separates prediction error, TD error, novelty,
--      and explicit alert sources.
--   4. Norepinephrine delta is recorded as a gain budget, not content.
--   5. Thresholds stay data-driven and seedable for later calibration.
--
-- Rollback, if needed before live adoption:
--   BEGIN;
--   DROP TABLE IF EXISTS lc_firings;
--   DROP TABLE IF EXISTS lc_state;
--   DROP TABLE IF EXISTS lc_triggers;
--   DELETE FROM schema_version WHERE version = 67;
--   COMMIT;
--
-- IDEMPOTENT: IF NOT EXISTS guards object creation; seed rows use
-- INSERT OR IGNORE so repeated application does not duplicate state.

CREATE TABLE IF NOT EXISTS lc_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source_table TEXT NOT NULL CHECK(source_table IN ('cerebellum_predictions','bg_td_events','memory_events','other')),
    threshold_field TEXT,
    threshold_value REAL,
    default_ne_delta REAL NOT NULL DEFAULT 0.0,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_lc_triggers_source_table ON lc_triggers(source_table);

CREATE TABLE IF NOT EXISTS lc_firings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    trigger_id INTEGER,
    trigger_source_event_id INTEGER,
    surprise_magnitude REAL NOT NULL DEFAULT 0.0,
    ne_delta_applied REAL NOT NULL DEFAULT 0.0,
    mode TEXT NOT NULL CHECK(mode IN ('phasic','tonic_shift')),
    context_hash TEXT,
    notes TEXT,
    FOREIGN KEY (trigger_id) REFERENCES lc_triggers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_lc_firings_fired_at ON lc_firings(fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_lc_firings_agent_fired ON lc_firings(agent_id, fired_at);
CREATE INDEX IF NOT EXISTS idx_lc_firings_trigger_fired ON lc_firings(trigger_id, fired_at);

CREATE TABLE IF NOT EXISTS lc_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL CHECK(mode IN ('phasic_ready','tonic_high','tonic_mid','tonic_low')),
    ne_reservoir REAL NOT NULL DEFAULT 0.5,
    last_phasic_at TEXT,
    last_tonic_shift_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

INSERT OR IGNORE INTO lc_triggers
    (name, source_table, threshold_field, threshold_value, default_ne_delta, description)
VALUES
    ('cerebellum_high_pe', 'cerebellum_predictions', 'delta_forward', 0.5, 0.15,
     'Cerebellum prediction error above threshold; surprise source for phasic LC.'),
    ('bg_large_td_error', 'bg_td_events', 'delta', 0.6, 0.10,
     'Basal-ganglia TD error above threshold; value surprise source for LC.'),
    ('novel_entity_sighting', 'memory_events', 'event_type', NULL, 0.05,
     'Novel observation event, especially new entity sightings.'),
    ('explicit_user_alert', 'other', NULL, NULL, 0.20,
     'Manual or user-declared alert that should raise global NE readiness.');

INSERT OR IGNORE INTO lc_state (id, mode, ne_reservoir)
VALUES (1, 'tonic_mid', 0.5);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (67, 'locus coeruleus Phase 1: triggers, firings, single-row LC state',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
