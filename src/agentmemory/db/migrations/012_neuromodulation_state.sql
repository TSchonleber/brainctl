-- =============================================================================
-- Migration 012: Neuromodulation State -- Stores the current runtime neuromodulation context that adjusts brain.db's
-- learning rate, retrieval breadth, and consolidation aggressiveness based on
-- organizational state (normal, incident, sprint, strategic_planning, focused_work).
-- =============================================================================

PRAGMA foreign_keys = ON;

-- =============================================================================
-- NEUROMODULATION_STATE — single-row runtime parameter context
-- =============================================================================

CREATE TABLE IF NOT EXISTS neuromodulation_state (
    id INTEGER PRIMARY KEY DEFAULT 1,   -- single-row table, always id=1

    -- Current organizational mode
    org_state TEXT NOT NULL DEFAULT 'normal'
        CHECK(org_state IN ('normal', 'incident', 'sprint', 'strategic_planning', 'focused_work')),

    -- ─── Dopamine (confidence reinforcement on task outcome) ──────────────
    dopamine_signal        REAL NOT NULL DEFAULT 0.0,   -- -1.0 to +1.0, decays 1/3 per day
    confidence_boost_rate  REAL NOT NULL DEFAULT 0.10,  -- delta added per successful task outcome
    confidence_decay_rate  REAL NOT NULL DEFAULT 0.02,  -- delta removed per day (per consolidation)
    dopamine_last_fired_at TEXT,

    -- ─── Norepinephrine (arousal / retrieval breadth) ────────────────────
    arousal_level                REAL NOT NULL DEFAULT 0.3,       -- 0.0-1.0
    retrieval_breadth_multiplier REAL NOT NULL DEFAULT 1.0,       -- multiplied into result LIMIT
    consolidation_immediacy      TEXT NOT NULL DEFAULT 'scheduled'
                                     CHECK(consolidation_immediacy IN ('immediate', 'scheduled')),
    consolidation_interval_mins  INTEGER NOT NULL DEFAULT 240,    -- 4h default (normal ops)

    -- ─── Acetylcholine (focus / signal-to-noise / scope restriction) ─────
    focus_level                REAL NOT NULL DEFAULT 0.3,   -- 0.0-1.0
    similarity_threshold_delta REAL NOT NULL DEFAULT 0.0,   -- added to base threshold (0.70)
    scope_restriction          TEXT,                         -- NULL = global; 'project:X' = locked
    exploitation_bias          REAL NOT NULL DEFAULT 0.0,   -- 0.0-1.0 weight on recalled_count bonus

    -- ─── Serotonin (time horizon / patience / context depth) ─────────────
    temporal_lambda       REAL NOT NULL DEFAULT 0.030,   -- decay const: weight = exp(-λ * days)
    context_window_depth  INTEGER NOT NULL DEFAULT 50,   -- recent events injected into context

    -- ─── Metadata ─────────────────────────────────────────────────────────
    detected_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    detection_method TEXT NOT NULL DEFAULT 'auto'
                         CHECK(detection_method IN ('auto', 'manual', 'policy')),
    expires_at       TEXT,      -- for manual overrides; NULL = no expiry
    triggered_by     TEXT,      -- agent_id that last updated this state
    notes            TEXT
);

-- Enforce single-row invariant
CREATE UNIQUE INDEX IF NOT EXISTS idx_neuromod_singleton ON neuromodulation_state(id);

-- Seed default normal-ops state
INSERT OR IGNORE INTO neuromodulation_state (id) VALUES (1);

-- =============================================================================
-- NEUROMODULATION_TRANSITIONS — audit log of state changes
-- =============================================================================

CREATE TABLE IF NOT EXISTS neuromodulation_transitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_state       TEXT NOT NULL,
    to_state         TEXT NOT NULL,
    reason           TEXT,
    triggered_by     TEXT,   -- agent_id
    transitioned_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_neuromod_transitions_ts ON neuromodulation_transitions(transitioned_at DESC);

-- =============================================================================
-- SCHEMA VERSION
-- =============================================================================

INSERT INTO schema_version (version, description)
VALUES (12, 'Neuromodulation state — dynamic learning rate context based on org state ');
