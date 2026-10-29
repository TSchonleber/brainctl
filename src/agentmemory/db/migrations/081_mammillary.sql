-- Migration 081: mammillary bodies + Papez circuit — Phase 1 schema
--
-- The mammillary bodies are the Papez-circuit hub:
--   hippocampus → fornix → mammillary bodies → ATN (anterior thalamus)
--                       → cingulate → hippocampus
--
-- Damage produces Korsakoff syndrome — dense anterograde amnesia.
-- ATN-DAMAGE > MD-thalamus for that pattern. Mammillary bodies are
-- thus a specific bottleneck in episodic memory consolidation.
--
-- Existing brainctl has the broad hippocampus subsystem + (now) CA1
-- + Subiculum + anterior-thalamus-analog inside the thalamus module.
-- What's missing is the explicit Papez-loop transport: which memories
-- have made it through the (hippocampus → MB → ATN → cingulate)
-- circuit vs. which are still hippocampus-only.
--
-- Phase 1 ships:
--   mammillary_transit_log — log of episodic memories whose
--                            consolidation has passed through the Papez
--                            circuit at least once
--   mammillary_state — single row tracking transit count + recent rate
--
-- Phase 2 will auto-log Papez transit on consolidation_run for
-- episodic memories. Phase 3 will let Papez-completed memories surface
-- with higher confidence in retrieval (proxy for "consolidated into
-- declarative knowledge").
--
-- Rollback:
--   DROP TABLE IF EXISTS mammillary_transit_log;
--   DROP TABLE IF EXISTS mammillary_state;
--   DELETE FROM schema_version WHERE version = 81;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS mammillary_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_transits INTEGER NOT NULL DEFAULT 0,
    transits_24h INTEGER NOT NULL DEFAULT 0,
    last_transit_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO mammillary_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS mammillary_transit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transited_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    memory_id INTEGER NOT NULL,
    agent_id TEXT,
    direction TEXT NOT NULL CHECK(direction IN (
        'hippocampus_to_atn',     -- forward leg of Papez
        'atn_to_cingulate',       -- top-down
        'cingulate_to_hippocampus', -- closing the loop
        'full_loop'                  -- single full Papez circuit completion
    )),
    transit_strength REAL NOT NULL DEFAULT 1.0 CHECK(transit_strength BETWEEN 0.0 AND 1.0),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_mtl_recent ON mammillary_transit_log(transited_at);
CREATE INDEX IF NOT EXISTS idx_mtl_memory ON mammillary_transit_log(memory_id, transited_at);
CREATE INDEX IF NOT EXISTS idx_mtl_direction ON mammillary_transit_log(direction, transited_at);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (81, 'mammillary bodies + Papez circuit Phase 1: 2 tables (state + transit log)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
