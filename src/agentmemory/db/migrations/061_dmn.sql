-- Migration 061: DMN — offline simulation / counterfactual rollouts
-- Schacter's "constructive episodic simulation" — recombine memories+entities
-- into plausible futures. Speculative memories live in a QUARANTINED table
-- (no FTS5, no vector index) so they never poison default retrieval.

CREATE TABLE IF NOT EXISTS dmn_simulations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    seed_type TEXT NOT NULL CHECK(seed_type IN ('entity','memory','event')),
    seed_id INTEGER NOT NULL,
    scope TEXT,
    scenario TEXT NOT NULL,
    plausibility REAL NOT NULL DEFAULT 0.5,
    novelty REAL NOT NULL DEFAULT 0.5,
    utility REAL NOT NULL DEFAULT 0.5,
    composite_score REAL NOT NULL DEFAULT 0.5,
    triggered_by TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    retired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dmn_sims_agent ON dmn_simulations(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dmn_sims_score ON dmn_simulations(composite_score DESC);

-- QUARANTINED — no FTS5/vec triggers, no default retrieval visibility.
CREATE TABLE IF NOT EXISTS dmn_speculative_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id INTEGER NOT NULL REFERENCES dmn_simulations(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT,
    scope TEXT,
    confidence REAL NOT NULL DEFAULT 0.3,
    validation_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(validation_state IN ('pending','corroborated','falsified','expired')),
    validated_against_event_id INTEGER,
    promoted_memory_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dmn_spec_state ON dmn_speculative_memories(validation_state, expires_at);

CREATE TABLE IF NOT EXISTS dmn_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    next_run_at TEXT,
    last_run_at TEXT,
    idle_threshold_s INTEGER NOT NULL DEFAULT 600,
    new_mem_threshold INTEGER NOT NULL DEFAULT 100,
    max_sims_per_run INTEGER NOT NULL DEFAULT 5,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(agent_id)
);
INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (61, 'DMN: offline simulation + quarantined speculative memories', strftime('%Y-%m-%dT%H:%M:%S','now'));
