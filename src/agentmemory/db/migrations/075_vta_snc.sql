-- Migration 075: VTA/SNc dopamine source — Phase 1 schema
--
-- Avenue 7 from research/autonomous-research-avenues-2026-05-20.md.
-- Currently dopamine in brainctl exists as a *dial*
-- (bg_modulators.tonic_da) and a *broadcast* (bg_td_events.delta).
-- What's missing is the **nucleus** that sources the signal with its
-- own state and firing log.
--
-- This migration adds:
--   vta_firings — log of phasic dopamine events (the nucleus's
--                 actual firing) with magnitude + source
--   vta_state — single row tracking tonic baseline, phasic count,
--               authentication-style "burst budget" (depletes per
--               firing, refills with time)
--   vta_pathway_links — VTA projects to many targets; this catalogs
--                       which downstream subsystems receive DA from VTA
--                       vs SNc (Mesolimbic, Mesocortical, Nigrostriatal)
--
-- Pairs with Habenula (PR #124, migration 070): Habenula's
-- suggested_da_damp is the input habenula side; VTA tracks the output
-- side. Phase 3 connects them — Habenula damping reduces VTA tonic.
--
-- Rollback:
--   DROP TABLE IF EXISTS vta_pathway_links;
--   DROP TABLE IF EXISTS vta_firings;
--   DROP TABLE IF EXISTS vta_state;
--   DELETE FROM schema_version WHERE version = 75;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS vta_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tonic_da REAL NOT NULL DEFAULT 0.5 CHECK(tonic_da BETWEEN 0.0 AND 1.0),
    phasic_burst REAL NOT NULL DEFAULT 0.0 CHECK(phasic_burst BETWEEN 0.0 AND 1.0),
    burst_budget REAL NOT NULL DEFAULT 1.0 CHECK(burst_budget BETWEEN 0.0 AND 1.0),
    pathology_flag TEXT CHECK(pathology_flag IN ('none', 'low_da', 'high_da') OR pathology_flag IS NULL),
    last_phasic_at TEXT,
    last_tonic_update_at TEXT,
    total_firings INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO vta_state (id, pathology_flag) VALUES (1, 'none');

CREATE TABLE IF NOT EXISTS vta_firings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    burst_magnitude REAL NOT NULL CHECK(burst_magnitude BETWEEN 0.0 AND 1.0),
    source_kind TEXT NOT NULL CHECK(source_kind IN (
        'bg_td_positive', 'novelty', 'reward_received', 'explicit_motivation', 'other'
    )),
    source_event_id INTEGER,
    target_pathway TEXT CHECK(target_pathway IN (
        'mesolimbic', 'mesocortical', 'nigrostriatal', 'broadcast', 'other'
    )),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_vta_recent ON vta_firings(fired_at);
CREATE INDEX IF NOT EXISTS idx_vta_pathway ON vta_firings(target_pathway, fired_at);
CREATE INDEX IF NOT EXISTS idx_vta_source ON vta_firings(source_kind, fired_at);

CREATE TABLE IF NOT EXISTS vta_pathway_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pathway TEXT NOT NULL CHECK(pathway IN ('mesolimbic', 'mesocortical', 'nigrostriatal', 'broadcast')),
    target_subsystem TEXT NOT NULL,
    description TEXT,
    UNIQUE (pathway, target_subsystem)
);

INSERT OR IGNORE INTO vta_pathway_links (pathway, target_subsystem, description) VALUES
    ('mesolimbic', 'nucleus_accumbens', 'reward-seeking / motivational salience (NAc-analog in BG)'),
    ('mesolimbic', 'amygdala', 'salience tagging — DA boosts amygdala valence updates'),
    ('mesocortical', 'pfc', 'PFC working memory + executive — DA gates PBWM updates'),
    ('mesocortical', 'acc', 'effort / cost-of-control modulation'),
    ('nigrostriatal', 'basal_ganglia', 'striatal Go/NoGo learning — primary RL training signal'),
    ('broadcast', 'bg_modulators', 'global tonic_da dial — every reader sees the modulation');

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (75, 'VTA/SNc Phase 1: dopamine source structure (3 tables, 6 pathway-link seeds)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
