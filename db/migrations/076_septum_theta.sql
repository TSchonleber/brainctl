-- Migration 076: medial septum + theta rhythm — Phase 1 schema
--
-- Avenue 8 from research/autonomous-research-avenues-2026-05-20.md.
-- Medial septum is the hippocampal theta pacemaker (4-8 Hz rhythm).
-- The cmd_search docstring already mentions "theta-gamma coupling"
-- ("Result count is capped at 7 × agent attention_budget_tier") but
-- there's no actual theta clock.
--
-- Phase 1 ships:
--   septum_state — single row tracking current phase + bin + cycle count
--   septum_ticks — log of theta-cycle ticks (heartbeat)
--   septum_phase_locked_memories — index of which theta bin each
--                                  memory was written/recalled in
--
-- Phase 1 = manual tick advancement + queries. Phase 2 = daemon-driven
-- automatic ticking on a configurable cadence. Phase 3 = phase-locked
-- memory_search (only memories from the current theta bin).
--
-- Rollback:
--   DROP TABLE IF EXISTS septum_phase_locked_memories;
--   DROP TABLE IF EXISTS septum_ticks;
--   DROP TABLE IF EXISTS septum_state;
--   DELETE FROM schema_version WHERE version = 76;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS septum_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    theta_frequency_hz REAL NOT NULL DEFAULT 6.0 CHECK(theta_frequency_hz BETWEEN 4.0 AND 8.0),
    theta_phase REAL NOT NULL DEFAULT 0.0 CHECK(theta_phase BETWEEN 0.0 AND 6.283185307),  -- radians
    theta_bin INTEGER NOT NULL DEFAULT 0 CHECK(theta_bin BETWEEN 0 AND 7),  -- 8 bins per cycle (45°)
    cycle_count INTEGER NOT NULL DEFAULT 0,
    last_tick_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,  -- 0=disabled, 1=enabled (Phase 2 daemon flag)
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO septum_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS septum_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    cycle_count INTEGER NOT NULL,
    theta_bin INTEGER NOT NULL,
    triggered_by TEXT  -- 'manual' | 'daemon' | 'aras_signal'
);
CREATE INDEX IF NOT EXISTS idx_septum_ticks_recent ON septum_ticks(ticked_at);
CREATE INDEX IF NOT EXISTS idx_septum_ticks_cycle ON septum_ticks(cycle_count);

CREATE TABLE IF NOT EXISTS septum_phase_locked_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL,
    locked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    theta_bin INTEGER NOT NULL,
    cycle_count INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('write', 'recall', 'reconsolidate')),
    UNIQUE (memory_id, locked_at, operation)
);
CREATE INDEX IF NOT EXISTS idx_splm_bin ON septum_phase_locked_memories(theta_bin, locked_at);
CREATE INDEX IF NOT EXISTS idx_splm_memory ON septum_phase_locked_memories(memory_id);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (76, 'septum + theta rhythm Phase 1: 3 tables for hippocampal theta pacemaker',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
