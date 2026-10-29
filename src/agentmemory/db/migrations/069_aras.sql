-- Migration 069: ascending reticular activating system — Phase 1 schema
--
-- The brainstem-level global arousal broadcast. Sits ABOVE LC + NB —
-- ARAS gates whether the rest of the neuromod surface is responsive
-- at all (anesthesia is functionally an ARAS shutdown; waking is
-- ARAS ramping up).
--
-- Phase 1 is inspection-only / additive: schema + read+CRUD tools.
-- Does not yet modulate LC/NB/retrieval. That's Phase 3.
--
-- Four biological invariants encoded:
--   1. Tonic vs phasic separation (sustained drive + brief pulses).
--   2. Discrete sleep/wake regimes (not just a scalar).
--   3. Recovery from suppression takes time (last_transition_at).
--   4. Event classes drive specific arousal deltas (seed catalog).
--
-- Rollback:
--   DROP TABLE IF EXISTS aras_transitions;
--   DROP TABLE IF EXISTS aras_state;
--   DROP TABLE IF EXISTS aras_triggers;
--   DELETE FROM schema_version WHERE version = 69;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS aras_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    trigger_kind TEXT NOT NULL CHECK(trigger_kind IN (
        'novelty', 'threat', 'explicit_alert', 'consolidation_signal', 'idle_decay', 'other'
    )),
    default_arousal_delta REAL NOT NULL DEFAULT 0.05,
    default_target_mode TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_aras_triggers_kind ON aras_triggers(trigger_kind);

CREATE TABLE IF NOT EXISTS aras_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sleep_wake_mode TEXT NOT NULL DEFAULT 'awake_relaxed' CHECK(sleep_wake_mode IN (
        'nrem_sleep', 'rem_sleep', 'drowsy', 'awake_relaxed', 'awake_focused', 'hyperalert'
    )),
    arousal_level REAL NOT NULL DEFAULT 0.5 CHECK(arousal_level BETWEEN 0.0 AND 1.0),
    tonic_drive REAL NOT NULL DEFAULT 0.5 CHECK(tonic_drive BETWEEN 0.0 AND 1.0),
    phasic_alertness REAL NOT NULL DEFAULT 0.0 CHECK(phasic_alertness BETWEEN 0.0 AND 1.0),
    last_transition_at TEXT,
    last_drive_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO aras_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS aras_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transitioned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    from_mode TEXT NOT NULL,
    to_mode TEXT NOT NULL,
    reason TEXT,
    trigger_id INTEGER,
    arousal_before REAL,
    arousal_after REAL,
    notes TEXT,
    FOREIGN KEY (trigger_id) REFERENCES aras_triggers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_aras_transitions_recent ON aras_transitions(transitioned_at);
CREATE INDEX IF NOT EXISTS idx_aras_transitions_agent ON aras_transitions(agent_id, transitioned_at);
CREATE INDEX IF NOT EXISTS idx_aras_transitions_to_mode ON aras_transitions(to_mode, transitioned_at);

INSERT OR IGNORE INTO aras_triggers (name, trigger_kind, default_arousal_delta, default_target_mode, description) VALUES
    ('novel_query', 'novelty', 0.05, 'awake_focused', 'previously-unseen query pattern — gentle arousal nudge'),
    ('high_pe_event', 'novelty', 0.10, 'awake_focused', 'cerebellum_predictions delta_forward above threshold'),
    ('consolidation_complete', 'consolidation_signal', -0.10, 'drowsy', 'dream cycle finished — permits arousal taper'),
    ('idle_30min', 'idle_decay', -0.05, 'drowsy', 'no agent activity for 30 min'),
    ('explicit_user_alert', 'explicit_alert', 0.30, 'hyperalert', 'user-flagged urgent input');

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (69, 'ARAS Phase 1: 3 tables (triggers, state, transitions) + 5 seed trigger classes',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
