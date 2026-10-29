-- Migration 024: Bayesian confidence wiring — confidence_alpha/beta aliases + behavioral notes -- Author: Engram (Memory Systems Lead)
-- Date: 2026-03-28
-- Purpose: Expose confidence_alpha and confidence_beta as virtual generated columns so that
--          downstream agents AGM belief revision) can query either alpha/beta
--          (Epoch's canonical names) or confidence_alpha/confidence_beta naming).
--          Behavioral wiring (recall→α++, contradiction→β++, decay→β++) is done in code.
-- Note: alpha/beta columns were added in migration 020 , by Epoch) with a 2x prior.
--       This migration adds the aliases and updates schema_version.
-- References: , , -- Schema version: 23 -> 24

-- Add generated column aliases so both naming conventions work.
-- confidence_alpha = alpha (evidence for), confidence_beta = beta (evidence against)
ALTER TABLE memories ADD COLUMN confidence_alpha REAL GENERATED ALWAYS AS (alpha) VIRTUAL;
ALTER TABLE memories ADD COLUMN confidence_beta  REAL GENERATED ALWAYS AS (beta)  VIRTUAL;

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (24, datetime('now'),
    'confidence_alpha/beta generated columns aliasing alpha/beta; Bayesian recall+contradiction+decay wiring ');

PRAGMA user_version = 24;
