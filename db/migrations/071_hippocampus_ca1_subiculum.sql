-- Migration 071: hippocampus CA1 + Subiculum — Phase 1 schema
--
-- Completes the hippocampal trisynaptic loop. Migration 059 shipped
-- DG (pattern separation) + CA3 (pattern completion); this migration
-- adds CA1 (match/mismatch detector) + Subiculum (cortical bridge).
--
-- Phase 1 is inspection-only / additive. Tables + tools only. Phase 2
-- hooks CA1 into the existing hippocampus_* pipeline. Phase 3 wires
-- Subiculum into workspace_broadcasts. Phase 4 enforces.
--
-- Rollback:
--   DROP TABLE IF EXISTS hippocampus_subiculum_outputs;
--   DROP TABLE IF EXISTS hippocampus_ca1_state;
--   DROP TABLE IF EXISTS hippocampus_ca1_comparisons;
--   DELETE FROM schema_version WHERE version = 71;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS hippocampus_ca1_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    compared_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    memory_id INTEGER,
    ec_input_hash TEXT,
    ca3_output_hash TEXT,
    match_score REAL NOT NULL CHECK(match_score BETWEEN 0.0 AND 1.0),
    novelty_score REAL NOT NULL CHECK(novelty_score BETWEEN 0.0 AND 1.0),
    classification TEXT NOT NULL CHECK(classification IN ('match', 'mismatch', 'partial', 'ambiguous')),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_ca1_cmp_recent ON hippocampus_ca1_comparisons(compared_at);
CREATE INDEX IF NOT EXISTS idx_ca1_cmp_agent ON hippocampus_ca1_comparisons(agent_id, compared_at);
CREATE INDEX IF NOT EXISTS idx_ca1_cmp_class ON hippocampus_ca1_comparisons(classification, compared_at);

CREATE TABLE IF NOT EXISTS hippocampus_ca1_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    recent_match_rate REAL NOT NULL DEFAULT 0.5 CHECK(recent_match_rate BETWEEN 0.0 AND 1.0),
    recent_novelty_rate REAL NOT NULL DEFAULT 0.5 CHECK(recent_novelty_rate BETWEEN 0.0 AND 1.0),
    total_comparisons INTEGER NOT NULL DEFAULT 0,
    last_comparison_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO hippocampus_ca1_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS hippocampus_subiculum_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    memory_id INTEGER,
    ca1_comparison_id INTEGER,
    target_channel TEXT NOT NULL CHECK(target_channel IN ('cortex_general', 'workspace_broadcast', 'thalamus_relay', 'other')),
    output_strength REAL NOT NULL DEFAULT 0.5 CHECK(output_strength BETWEEN 0.0 AND 1.0),
    notes TEXT,
    FOREIGN KEY (ca1_comparison_id) REFERENCES hippocampus_ca1_comparisons(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_outputs_recent ON hippocampus_subiculum_outputs(output_at);
CREATE INDEX IF NOT EXISTS idx_sub_outputs_target ON hippocampus_subiculum_outputs(target_channel, output_at);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (71, 'hippocampus CA1 + Subiculum Phase 1: 3 tables completing the trisynaptic loop',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
