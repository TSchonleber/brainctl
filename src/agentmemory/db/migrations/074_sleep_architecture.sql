-- Migration 074: sleep architecture — Phase 1 schema
--
-- Operationalizes Avenue 1 from research/autonomous-research-avenues-2026-05-20.md:
-- "Sleep architecture as a first-class state machine." Biology
-- partitions sleep into NREM 1/2/3 + REM, each with qualitatively
-- different memory operations. brainctl's dream_cycle + DMN treat
-- sleep as one undifferentiated state.
--
-- Phase 1 ships:
--   sleep_cycle_state — current cycle + stage + entry time
--   sleep_cycle_transitions — log of stage transitions with cause
--   sleep_stage_catalog — per-stage description + permitted operations
--
-- The catalog encodes what each stage *can* do (NREM2 = spindles +
-- declarative consolidation; NREM3/SWS = sharp-wave ripples + replay;
-- REM = procedural / emotional consolidation + bisociation).
--
-- Phase 1 is inspection + manual writes. Phase 2 auto-progresses
-- through ultradian cycles when ARAS sleep_wake_mode = nrem/rem_sleep.
-- Phase 3 stage-gates consolidation operations.
--
-- Rollback:
--   DROP TABLE IF EXISTS sleep_cycle_transitions;
--   DROP TABLE IF EXISTS sleep_cycle_state;
--   DROP TABLE IF EXISTS sleep_stage_catalog;
--   DELETE FROM schema_version WHERE version = 74;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS sleep_stage_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL UNIQUE CHECK(stage IN ('nrem1', 'nrem2', 'nrem3_sws', 'rem', 'awake')),
    typical_duration_seconds INTEGER NOT NULL DEFAULT 600,
    description TEXT,
    permitted_operations TEXT,    -- comma-separated tags (e.g. 'spindle_consolidation,replay,bisociation')
    arousal_floor REAL NOT NULL DEFAULT 0.0,
    arousal_ceiling REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS sleep_cycle_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_stage TEXT NOT NULL DEFAULT 'awake' CHECK(current_stage IN ('nrem1', 'nrem2', 'nrem3_sws', 'rem', 'awake')),
    cycle_number INTEGER NOT NULL DEFAULT 0,         -- ultradian cycle count (each ~90 min in biology)
    stage_entered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    cycle_started_at TEXT,
    total_sleep_seconds INTEGER NOT NULL DEFAULT 0,
    total_rem_seconds INTEGER NOT NULL DEFAULT 0,
    total_sws_seconds INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO sleep_cycle_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS sleep_cycle_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transitioned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    from_stage TEXT NOT NULL,
    to_stage TEXT NOT NULL,
    cycle_number INTEGER NOT NULL,
    duration_in_from_stage_seconds INTEGER,
    reason TEXT,
    triggered_by TEXT  -- 'manual' | 'aras_signal' | 'duration_elapsed' | 'consolidation_complete'
);
CREATE INDEX IF NOT EXISTS idx_sct_recent ON sleep_cycle_transitions(transitioned_at);
CREATE INDEX IF NOT EXISTS idx_sct_to_stage ON sleep_cycle_transitions(to_stage, transitioned_at);
CREATE INDEX IF NOT EXISTS idx_sct_cycle ON sleep_cycle_transitions(cycle_number);

INSERT OR IGNORE INTO sleep_stage_catalog (stage, typical_duration_seconds, description, permitted_operations, arousal_floor, arousal_ceiling) VALUES
    ('awake', 0, 'normal operating state — full retrieval and writes enabled', 'all', 0.30, 1.0),
    ('nrem1', 300, 'sleep onset — light, easily aroused; no canonical memory op', 'idle_decay', 0.15, 0.40),
    ('nrem2', 1500, 'spindle stage — sleep spindles + slow oscillations; declarative consolidation', 'spindle_consolidation,semantic_promotion', 0.10, 0.30),
    ('nrem3_sws', 1200, 'slow-wave sleep — sharp-wave ripples; hippocampus→neocortex replay', 'swr_replay,episodic_to_semantic,memory_promote', 0.05, 0.20),
    ('rem', 900, 'REM — procedural + emotional consolidation; bisociative recombination; dreaming', 'procedural_consolidation,bisociation,dmn_simulate,reconsolidate', 0.20, 0.50);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (74, 'sleep architecture Phase 1: 3 tables (catalog + state + transitions) with 5 seeded stages',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
