-- Migration 021: Agent Expertise — Brier Score Calibration Column -- Author: Sentinel 2 (Memory Integrity Monitor)
-- Date: 2026-03-28
-- Purpose: Add brier_score calibration column to agent_expertise table.
--          Brier score (0.0 = perfectly calibrated, 2.0 = worst) tracks
--          how well an agent's stated confidence matches actual outcomes.
--          Updated via: brainctl expertise update <agent_id> <domain> --brier <score>
-- References: (Social Epistemology Phase 1), (Wave 10 research)
-- Schema version: 20 -> 21

ALTER TABLE agent_expertise ADD COLUMN brier_score REAL DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_expertise_brier ON agent_expertise(brier_score)
    WHERE brier_score IS NOT NULL;

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (21, datetime('now'),
    'agent_expertise.brier_score — Brier calibration score column for source-weighted recall ');

PRAGMA user_version = 21;
