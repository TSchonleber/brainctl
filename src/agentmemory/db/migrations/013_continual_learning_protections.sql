-- Migration 013: Continual Learning Protections
-- Author: Sentinel 2 (Memory Integrity Monitor)
-- Date: 2026-03-28
-- Purpose: Prevent high-value memories from being corrupted by routine consolidation.
--          Implements importance locking via a `protected` flag. Protected memories
--          are skipped during demotion, retirement, compression, and cluster-merge.
-- References: research/wave6/24_continual_learning_catastrophic_forgetting.md -- (implementation)
-- Schema version: 12 -> 13

-- protected: 1 = memory is importance-locked and must survive consolidation passes.
-- Set automatically by hippocampus when recalled_count >= 10 AND confidence >= 0.8.
-- Can also be set manually by a trusted agent or board user.
ALTER TABLE memories ADD COLUMN protected INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_memories_protected ON memories(protected) WHERE protected = 1;

-- Backfill: mark currently qualifying memories as protected.
UPDATE memories
SET protected = 1
WHERE retired_at IS NULL
  AND recalled_count >= 10
  AND confidence >= 0.8;

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (13, datetime('now'),
  'protected flag for importance-locked memories — continual learning protections ');

PRAGMA user_version = 13;
