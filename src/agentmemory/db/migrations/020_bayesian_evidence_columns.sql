-- Migration 020: Bayesian Brain Phase 1 — α/β Evidence Columns -- Author: Epoch (Temporal Cognition Engineer)
-- Date: 2026-03-28
-- Purpose: Add alpha and beta columns to memories table as Beta distribution parameters.
--          alpha = evidence for (successes), beta = evidence against (failures).
--          Phase 1 only: schema + backfill. No behavioral changes until Phase 2 wires
--          recall events → α increment and contradiction events → β increment.
-- Formula:
--   alpha = confidence * 2.0   (weak prior, total_evidence = 2.0)
--   beta  = (1.0 - confidence) * 2.0
--   so that confidence ≈ alpha / (alpha + beta) is preserved.
-- References: (Wave 10 Bayesian Brain research), -- Schema version: 19 -> 20

ALTER TABLE memories ADD COLUMN alpha REAL DEFAULT 1.0;
ALTER TABLE memories ADD COLUMN beta  REAL DEFAULT 1.0;

CREATE INDEX idx_memories_alpha ON memories(alpha) WHERE retired_at IS NULL;
CREATE INDEX idx_memories_beta  ON memories(beta)  WHERE retired_at IS NULL;

-- Backfill all existing rows from current confidence scalar.
-- NULL confidence defaults to 1.0 (full confidence → alpha=2.0, beta=0.0 effectively,
-- but we clamp beta to a minimum of 0.0 to avoid negative values).
UPDATE memories
SET
    alpha = ROUND(COALESCE(confidence, 1.0) * 2.0, 6),
    beta  = ROUND(MAX(0.0, (1.0 - COALESCE(confidence, 1.0)) * 2.0), 6);

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (20, datetime('now'),
    'memories.alpha + beta — Bayesian Beta distribution evidence columns, Phase 1 ');

PRAGMA user_version = 20;
