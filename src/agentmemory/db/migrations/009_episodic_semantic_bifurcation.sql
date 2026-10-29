-- Migration 009: Episodic/Semantic Memory Bifurcation
-- Author: Engram (Memory Systems Lead)
-- Date: 2026-03-28
-- Purpose: Add memory_type column distinguishing episodic (time-bound event records)
--          from semantic (stable abstracted facts). Enables type-aware decay, consolidation,
--          and the episodic-to-semantic promotion pass in hippocampus.py.
--          Also backfills supporting columns previously added directly to the live db.
-- References: research/wave3/01_episodic_semantic_bifurcation.md -- (episodic-to-semantic promotion implementation)
-- Schema version: 8 -> 9

-- memory_type: 'episodic' (default) or 'semantic'
ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'episodic'
  CHECK(memory_type IN ('episodic','semantic'));

-- Provenance: JSON list of memory IDs this memory was synthesized or derived from
ALTER TABLE memories ADD COLUMN derived_from_ids TEXT;

-- Soft-retraction support (distinct from retired_at — retraction implies error correction)
ALTER TABLE memories ADD COLUMN retracted_at TEXT;
ALTER TABLE memories ADD COLUMN retraction_reason TEXT;

-- Trust / validation fields
ALTER TABLE memories ADD COLUMN validation_agent_id TEXT REFERENCES agents(id);
ALTER TABLE memories ADD COLUMN validated_at TEXT;
ALTER TABLE memories ADD COLUMN trust_score REAL DEFAULT 1.0;

-- Index for type-filtered queries (decay, promotion, retrieval)
CREATE INDEX idx_memories_type ON memories(memory_type);
CREATE INDEX idx_memories_trust_score ON memories(trust_score);
CREATE INDEX idx_memories_retracted ON memories(retracted_at) WHERE retracted_at IS NOT NULL;
CREATE INDEX idx_memories_validation ON memories(validation_agent_id);

-- Heuristic backfill: assign 'semantic' to permanent/long memories with knowledge-stable categories
-- All others remain 'episodic' (the default). This is a best-effort classification.
UPDATE memories
SET memory_type = 'semantic'
WHERE retired_at IS NULL
  AND temporal_class IN ('permanent', 'long')
  AND category IN ('identity', 'environment', 'convention', 'integration', 'preference');

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (9, datetime('now'),
  'memory_type column (episodic|semantic) + derived_from_ids, retraction, trust fields — episodic/semantic bifurcation , ');

PRAGMA user_version = 9;
