-- Migration 080: superior + inferior colliculi — Phase 1 schema
--
-- Avenue 9 from research/autonomous-research-avenues-2026-05-20.md.
-- Subcortical orienting reflex: superior colliculus (SC) = visual /
-- attention orienting; inferior colliculus (IC) = auditory orienting.
-- They fire BEFORE cortical processing and bias attention rapidly.
--
-- brainctl analog: pre-cortical orienting on novel-pattern signals
-- (new entity sightings, unfamiliar query shapes, unusual content
-- types). Fires a fast ARAS drive pulse + thalamic mode adjustment
-- before the full retrieval pipeline gets going.
--
-- Phase 1 ships:
--   colliculi_orienting_events — log of pre-cortical orient events
--   colliculi_state — single row tracking SC/IC tonic activity
--   colliculi_trigger_patterns — pattern catalog (which novel shapes
--                                fire which sub-nucleus)
--
-- Phase 2 wires into MCP dispatch as a sub-millisecond early-fire
-- before BG/cerebellum consults. Phase 3 modulates ARAS + thalamus
-- in response.
--
-- Rollback:
--   DROP TABLE IF EXISTS colliculi_trigger_patterns;
--   DROP TABLE IF EXISTS colliculi_orienting_events;
--   DROP TABLE IF EXISTS colliculi_state;
--   DELETE FROM schema_version WHERE version = 80;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS colliculi_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sc_tonic REAL NOT NULL DEFAULT 0.3 CHECK(sc_tonic BETWEEN 0.0 AND 1.0),
    ic_tonic REAL NOT NULL DEFAULT 0.3 CHECK(ic_tonic BETWEEN 0.0 AND 1.0),
    total_orienting_events INTEGER NOT NULL DEFAULT 0,
    last_orient_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO colliculi_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS colliculi_trigger_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sub_nucleus TEXT NOT NULL CHECK(sub_nucleus IN ('sc', 'ic')),
    pattern_kind TEXT NOT NULL CHECK(pattern_kind IN (
        'novel_entity_shape', 'unfamiliar_query_form', 'unusual_content_type',
        'sudden_volume_change', 'cross_modal_mismatch', 'other'
    )),
    default_strength REAL NOT NULL DEFAULT 0.4 CHECK(default_strength BETWEEN 0.0 AND 1.0),
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

INSERT OR IGNORE INTO colliculi_trigger_patterns (name, sub_nucleus, pattern_kind, default_strength, description) VALUES
    ('new_entity_seen', 'sc', 'novel_entity_shape', 0.5, 'previously-unseen entity name pattern'),
    ('unusual_query_structure', 'sc', 'unfamiliar_query_form', 0.4, 'query token sequence doesn''t match recent distribution'),
    ('content_type_shift', 'sc', 'unusual_content_type', 0.3, 'incoming content uses category not seen in last 7d'),
    ('audio_burst', 'ic', 'sudden_volume_change', 0.6, 'audio input event with sharp amplitude'),
    ('cross_modal_disagree', 'ic', 'cross_modal_mismatch', 0.5, 'auditory + visual signals disagree about same target');

CREATE TABLE IF NOT EXISTS colliculi_orienting_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    oriented_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    sub_nucleus TEXT NOT NULL CHECK(sub_nucleus IN ('sc', 'ic')),
    pattern_id INTEGER REFERENCES colliculi_trigger_patterns(id) ON DELETE SET NULL,
    strength REAL NOT NULL CHECK(strength BETWEEN 0.0 AND 1.0),
    target_description TEXT,
    aras_drive_fired INTEGER NOT NULL DEFAULT 0,    -- 1 if downstream ARAS was nudged
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_coe_recent ON colliculi_orienting_events(oriented_at);
CREATE INDEX IF NOT EXISTS idx_coe_subnucleus ON colliculi_orienting_events(sub_nucleus, oriented_at);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (80, 'colliculi Phase 1: SC/IC orienting reflex (3 tables, 5 seeded patterns)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
