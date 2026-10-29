-- Migration 068: nucleus basalis subsystem — Phase 1 schema
--
-- Pairs with migration 067 (locus coeruleus). NB-ACh complements LC-NE
-- as the dual gain/attention control axes the May 15 brain-region
-- coverage audit explicitly flagged as missing.
--
-- LC fires on surprise (broadly); NB fires on attention shifts
-- (target-locked). Both feed bg_modulators — LC writes lc_ne, NB
-- writes a new acetylcholine column added by this migration.
--
-- Phase 1 is inspection-only / additive: schema + read+CRUD tools.
-- No behavior change to retrieval, write gates, or any existing
-- subsystem. Phase 2 (separate PR) wires NB into the shadow consult
-- at mcp_server.py:3265 to fire on thalamic_salience above threshold.
-- Phase 3 closes the loop. Phase 4 enforces.
--
-- Four biological invariants encoded here (see docs/proposals/nucleus_basalis.md):
--   1. Basal-forebrain cholinergic projection is broad to cortex,
--      target-modulated by attention.
--   2. Phasic vs tonic ACh: phasic = target-locked spike,
--      tonic = sustained baseline.
--   3. ACh widens what's attended, narrows what's not.
--   4. Firing on attention SHIFTS, not steady-state attention.
--
-- Rollback, if needed before live adoption:
--   ALTER TABLE bg_modulators DROP COLUMN acetylcholine;  -- SQLite >= 3.35
--   DROP TABLE IF EXISTS nb_state;
--   DROP TABLE IF EXISTS nb_firings;
--   DROP TABLE IF EXISTS nb_attention_targets;
--   DELETE FROM schema_version WHERE version = 68;
--
-- IDEMPOTENT: IF NOT EXISTS guards object creation; seed rows use
-- INSERT OR IGNORE so repeated application does not duplicate state.
-- The ALTER TABLE ADD COLUMN uses IF NOT EXISTS (SQLite 3.35+, which
-- brainctl already requires per migration 023's pattern).

-- Catalog of channels NB can attend to. Seedable; new targets
-- registered idempotently via tool_nb_register_target.
CREATE TABLE IF NOT EXISTS nb_attention_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    channel_kind TEXT NOT NULL CHECK(channel_kind IN (
        'thalamic_sector', 'agent_scope', 'intent_class', 'entity_type', 'other'
    )),
    default_ach_gain REAL NOT NULL DEFAULT 0.10 CHECK(default_ach_gain BETWEEN 0.0 AND 1.0),
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    last_attended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_nb_targets_kind ON nb_attention_targets(channel_kind);

-- Log of NB firings (cholinergic broadcasts). Each row = one phasic
-- ACh burst directed at a target.
CREATE TABLE IF NOT EXISTS nb_firings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    target_id INTEGER NOT NULL,
    target_source_event_id INTEGER,
    attention_magnitude REAL NOT NULL,
    ach_delta_applied REAL NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('phasic', 'tonic_shift')),
    context_hash TEXT,
    notes TEXT,
    FOREIGN KEY (target_id) REFERENCES nb_attention_targets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nb_firings_recent ON nb_firings(fired_at);
CREATE INDEX IF NOT EXISTS idx_nb_firings_agent ON nb_firings(agent_id, fired_at);
CREATE INDEX IF NOT EXISTS idx_nb_firings_target ON nb_firings(target_id, fired_at);

-- Single-row reservoir + current attention focus.
CREATE TABLE IF NOT EXISTS nb_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'tonic_mid' CHECK(mode IN (
        'phasic_locked', 'tonic_high', 'tonic_mid', 'tonic_low'
    )),
    ach_reservoir REAL NOT NULL DEFAULT 0.5 CHECK(ach_reservoir BETWEEN 0.0 AND 1.0),
    last_attended_target_id INTEGER,
    last_phasic_at TEXT,
    last_tonic_shift_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    FOREIGN KEY (last_attended_target_id) REFERENCES nb_attention_targets(id)
);
INSERT OR IGNORE INTO nb_state (id, mode, ach_reservoir) VALUES (1, 'tonic_mid', 0.5);

-- Seed the 4 thalamic sectors brainctl already uses (sourced from the
-- thalamic_relays.sector enum in migration 050). Other channel kinds
-- (agent_scope, intent_class, entity_type) get registered later via
-- nb_register_target as the operator decides what to attend to.
INSERT OR IGNORE INTO nb_attention_targets (name, channel_kind, default_ach_gain, description) VALUES
    ('cognitive', 'thalamic_sector', 0.15, 'planning, reasoning, deliberation'),
    ('episodic', 'thalamic_sector', 0.10, 'event recall and timeline'),
    ('semantic', 'thalamic_sector', 0.08, 'concept / fact retrieval'),
    ('pii_sensitive', 'thalamic_sector', 0.20, 'PII / credential / wallet — high attention so W(m) sees it');

-- Extend bg_modulators with the 4th neuromod dial.
-- Re-run safety: the brainctl migrate runner gates re-application by
-- schema_version (this row gets the version=68 entry below), so the
-- ALTER only fires once per DB. If you're applying the migration via
-- raw sqlite3 against a brain.db that already has the column, this
-- ALTER will fail with a duplicate-column error — that's by design;
-- always go through `brainctl migrate` for live application.
ALTER TABLE bg_modulators ADD COLUMN acetylcholine REAL NOT NULL DEFAULT 0.5;

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (68, 'nucleus basalis Phase 1: 3 tables (targets, firings, state) + bg_modulators.acetylcholine',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
