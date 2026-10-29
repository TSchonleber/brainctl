-- Migration 018: EWC Importance Scoring -- Author: Engram (Memory Systems Lead)
-- Date: 2026-03-28
-- Purpose: Add ewc_importance column to memories for protecting high-value memories
--          from consolidation passes using an Elastic Weight Consolidation inspired score.
-- Formula: ewc_importance = 0.4 * norm_recalled_count + 0.4 * trust_score + 0.2 * norm_age
--   norm_recalled = MIN(1.0, recalled_count / 100.0)
--   trust_score   = already in [0, 1] (default 1.0)
--   norm_age      = MIN(1.0, age_days / 365.0)
-- References: (Continual Learning spec), -- Schema version: 17 -> 18

ALTER TABLE memories ADD COLUMN ewc_importance REAL NOT NULL DEFAULT 0.0;

CREATE INDEX idx_memories_ewc_importance ON memories(ewc_importance DESC) WHERE retired_at IS NULL;

-- Backfill ewc_importance for all active memories using SQL approximation of the formula.
-- Full Python recompute runs at each consolidation cycle via compute_ewc_importance().
UPDATE memories
SET ewc_importance = ROUND(
    0.4 * MIN(1.0, CAST(recalled_count AS REAL) / 100.0)
    + 0.4 * COALESCE(trust_score, 1.0)
    + 0.2 * MIN(1.0, (julianday('now') - julianday(created_at)) / 365.0),
    4
)
WHERE retired_at IS NULL;

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (18, datetime('now'),
  'ewc_importance scoring on memories — EWC protection for high-value memories ');

PRAGMA user_version = 18;
