-- Migration 077: raphe nuclei — Phase 1 schema
--
-- Serotonin source structure. Completes the neuromod-source trio
-- with LC (NE, migration 067) and VTA/SNc (DA, migration 075).
-- Currently serotonin in brainctl exists only as a dial
-- (bg_modulators.serotonin); there's no source nucleus with state.
--
-- Biology: dorsal raphe + median raphe nuclei produce most CNS
-- serotonin. 5-HT modulates patience, time horizon, mood persistence,
-- and the cost of waiting. Often framed as the "anti-impulsivity"
-- broadcaster. Low 5-HT correlates with impulsive / short-horizon
-- decisions; sustained high 5-HT extends the time horizon agents
-- will tolerate before giving up.
--
-- Phase 1 ships:
--   raphe_state — single row with tonic_5ht, phasic_burst,
--                time_horizon (seconds the system is willing to wait),
--                mood_baseline (sustained valence floor)
--   raphe_firings — log of phasic 5-HT events
--   raphe_subtype_catalog — DRN (dorsal) vs MRN (median) functional split
--
-- Phase 3 will wire raphe.time_horizon into BG's eligibility-trace decay
-- (high 5-HT → longer eligibility windows).
--
-- Rollback:
--   DROP TABLE IF EXISTS raphe_subtype_catalog;
--   DROP TABLE IF EXISTS raphe_firings;
--   DROP TABLE IF EXISTS raphe_state;
--   DELETE FROM schema_version WHERE version = 77;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS raphe_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tonic_5ht REAL NOT NULL DEFAULT 0.5 CHECK(tonic_5ht BETWEEN 0.0 AND 1.0),
    phasic_burst REAL NOT NULL DEFAULT 0.0 CHECK(phasic_burst BETWEEN 0.0 AND 1.0),
    time_horizon_seconds INTEGER NOT NULL DEFAULT 300 CHECK(time_horizon_seconds > 0),
    mood_baseline REAL NOT NULL DEFAULT 0.0 CHECK(mood_baseline BETWEEN -1.0 AND 1.0),
    last_phasic_at TEXT,
    total_firings INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO raphe_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS raphe_firings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    subtype TEXT NOT NULL CHECK(subtype IN ('drn', 'mrn')),
    magnitude REAL NOT NULL CHECK(magnitude BETWEEN 0.0 AND 1.0),
    trigger_kind TEXT CHECK(trigger_kind IN (
        'patience_required', 'sustained_effort', 'long_horizon_plan',
        'mood_stabilization', 'manual', 'other'
    ) OR trigger_kind IS NULL),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_raphe_recent ON raphe_firings(fired_at);
CREATE INDEX IF NOT EXISTS idx_raphe_subtype ON raphe_firings(subtype, fired_at);

CREATE TABLE IF NOT EXISTS raphe_subtype_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subtype TEXT NOT NULL UNIQUE CHECK(subtype IN ('drn', 'mrn')),
    description TEXT,
    target_subsystems TEXT,    -- comma-separated
    primary_effect TEXT
);
INSERT OR IGNORE INTO raphe_subtype_catalog (subtype, description, target_subsystems, primary_effect) VALUES
    ('drn', 'dorsal raphe nucleus — broad cortical + limbic projection', 'pfc,acc,amygdala,bg', 'time_horizon, patience, cost-of-waiting'),
    ('mrn', 'median raphe nucleus — hippocampus + septum projection', 'hippocampus,septum', 'mood persistence, contextual stability');

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (77, 'raphe nuclei Phase 1: serotonin source (3 tables, DRN+MRN subtype catalog)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
