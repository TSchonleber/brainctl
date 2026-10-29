-- ============================================================================
-- Unified Quantum Cognition Schema Migration (SQLite-compatible)
-- Adapted from: research/quantum/quantum_schema_migration.sql
-- — Engram
-- ============================================================================
-- Converts PostgreSQL-specific syntax to SQLite:
--   BYTEA → BLOB, BOOLEAN → INTEGER, UUID → TEXT,
--   NOW() → CURRENT_TIMESTAMP, gen_random_uuid() → hex(randomblob(16)),
--   OR REPLACE FUNCTION → omitted (not supported),
--   INTERVAL → datetime arithmetic
-- ============================================================================

BEGIN;

-- ============================================================================
-- PHASE 1: Add columns to memories table
-- ============================================================================

ALTER TABLE memories ADD COLUMN IF NOT EXISTS confidence_phase REAL DEFAULT 0.0;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS hilbert_projection BLOB DEFAULT NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS coherence_syndrome TEXT DEFAULT NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS decoherence_rate REAL DEFAULT NULL;

-- ============================================================================
-- PHASE 2: Add columns to agent_beliefs table
-- ============================================================================

ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS is_superposed INTEGER DEFAULT 0;
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS belief_density_matrix BLOB DEFAULT NULL;
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS coherence_score REAL DEFAULT 0.0;
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS entanglement_source_ids TEXT DEFAULT NULL;

-- ============================================================================
-- PHASE 3: recovery_candidates table -- ============================================================================

CREATE TABLE IF NOT EXISTS recovery_candidates (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  source_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  recoverable_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  syndrome TEXT NOT NULL,
  recovery_probability REAL NOT NULL,
  expected_fidelity REAL DEFAULT 0.0,
  last_recovery_attempt_at TEXT DEFAULT NULL,
  recovery_succeeded INTEGER DEFAULT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_recovery_candidates_source ON recovery_candidates(source_memory_id);
CREATE INDEX IF NOT EXISTS idx_recovery_candidates_recoverable ON recovery_candidates(recoverable_memory_id);
CREATE INDEX IF NOT EXISTS idx_recovery_candidates_probability ON recovery_candidates(recovery_probability DESC);

-- ============================================================================
-- PHASE 4: agent_entanglement table -- ============================================================================

CREATE TABLE IF NOT EXISTS agent_entanglement (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  agent_id_a TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  agent_id_b TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  entanglement_entropy REAL NOT NULL,
  reduced_entropy_a REAL DEFAULT 0.0,
  reduced_entropy_b REAL DEFAULT 0.0,
  shared_memory_count INTEGER DEFAULT 0,
  avg_shared_confidence REAL DEFAULT 0.0,
  bell_inequality_chsh REAL DEFAULT NULL,
  measured_at TEXT DEFAULT CURRENT_TIMESTAMP,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT agent_pair_order CHECK (agent_id_a < agent_id_b)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_entanglement_pair ON agent_entanglement(agent_id_a, agent_id_b);
CREATE INDEX IF NOT EXISTS idx_agent_entanglement_entropy ON agent_entanglement(entanglement_entropy DESC);

-- agent_ghz_groups table CREATE TABLE IF NOT EXISTS agent_ghz_groups (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  agent_ids TEXT NOT NULL,
  entangling_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  group_size INTEGER NOT NULL,
  ghz_violation_metric REAL DEFAULT NULL,
  collective_coherence REAL DEFAULT 0.0,
  measured_at TEXT DEFAULT CURRENT_TIMESTAMP,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_ghz_groups_memory ON agent_ghz_groups(entangling_memory_id);
CREATE INDEX IF NOT EXISTS idx_agent_ghz_groups_size ON agent_ghz_groups(group_size);

-- belief_collapse_events table CREATE TABLE IF NOT EXISTS belief_collapse_events (
  id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
  belief_id TEXT NOT NULL REFERENCES agent_beliefs(id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  collapsed_state TEXT NOT NULL,
  measured_amplitude REAL NOT NULL,
  collapse_type TEXT NOT NULL CHECK (collapse_type IN ('query', 'action', 'update')),
  collapse_context TEXT DEFAULT NULL,
  collapse_fidelity REAL DEFAULT 1.0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_belief_collapse_events_belief ON belief_collapse_events(belief_id);
CREATE INDEX IF NOT EXISTS idx_belief_collapse_events_agent ON belief_collapse_events(agent_id);
CREATE INDEX IF NOT EXISTS idx_belief_collapse_events_type ON belief_collapse_events(collapse_type);

-- ============================================================================
-- PHASE 5: Indexes for quantum operations
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_memories_confidence_phase ON memories(agent_id, confidence_phase)
  WHERE confidence_phase != 0.0;
CREATE INDEX IF NOT EXISTS idx_memories_decoherence_rate ON memories(decoherence_rate DESC)
  WHERE decoherence_rate IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_coherence_syndrome ON memories(agent_id)
  WHERE coherence_syndrome IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_beliefs_superposed ON agent_beliefs(agent_id, is_superposed)
  WHERE is_superposed = 1;
CREATE INDEX IF NOT EXISTS idx_agent_beliefs_coherence ON agent_beliefs(agent_id, coherence_score DESC)
  WHERE is_superposed = 1;
CREATE INDEX IF NOT EXISTS idx_agent_beliefs_entanglement_sources ON agent_beliefs(agent_id)
  WHERE entanglement_source_ids IS NOT NULL;

-- ============================================================================
-- PHASE 6: Views for quantum analysis (SQLite-adapted)
-- Note: stored functions (initialize_quantum_amplitudes, compute_entanglement_entropy,
--       detect_ghz_groups) omitted — SQLite does not support PL/pgSQL.
-- ============================================================================

DROP VIEW IF EXISTS superposed_beliefs;
CREATE VIEW superposed_beliefs AS
SELECT
  ab.id,
  ab.agent_id,
  ab.topic,
  ab.is_superposed,
  ab.coherence_score,
  ab.entanglement_source_ids,
  ab.created_at,
  ab.updated_at
FROM agent_beliefs ab
WHERE ab.is_superposed = 1;

DROP VIEW IF EXISTS entangled_agent_pairs;
CREATE VIEW entangled_agent_pairs AS
SELECT
  ae.agent_id_a,
  ae.agent_id_b,
  ae.entanglement_entropy,
  ae.bell_inequality_chsh,
  ae.shared_memory_count,
  ae.measured_at
FROM agent_entanglement ae
ORDER BY ae.entanglement_entropy DESC;

DROP VIEW IF EXISTS decoherent_memories;
CREATE VIEW decoherent_memories AS
SELECT
  id,
  content,
  confidence,
  coherence_syndrome,
  decoherence_rate,
  temporal_class,
  created_at,
  updated_at
FROM memories
WHERE coherence_syndrome IS NOT NULL
   OR decoherence_rate IS NOT NULL
ORDER BY decoherence_rate DESC;

DROP VIEW IF EXISTS recent_belief_collapses;
CREATE VIEW recent_belief_collapses AS
SELECT
  bce.id,
  bce.agent_id,
  bce.belief_id,
  bce.collapsed_state,
  bce.collapse_type,
  bce.collapse_fidelity,
  bce.created_at
FROM belief_collapse_events bce
WHERE bce.created_at > datetime('now', '-7 days')
ORDER BY bce.created_at DESC;

-- ============================================================================
-- PHASE 7: Initialize confidence_phase for existing memories (default already 0.0)
-- No explicit UPDATE needed — new DEFAULT handles it. Existing rows with NULL get 0.0.
-- ============================================================================

UPDATE memories SET confidence_phase = 0.0 WHERE confidence_phase IS NULL;

COMMIT;
