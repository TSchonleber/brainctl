-- ============================================================================
-- Unified Quantum Cognition Research Schema Migration
-- Wave 1 Consolidation: COS-379, COS-381, COS-382, COS-384
-- ============================================================================
-- Author: Superpose (consolidation), based on research from Qubit, Decohere, Entangle
-- Date: 2026-03-28
-- Purpose: Atomically merge 4 independent quantum schema proposals into brain.db
--
-- PROPOSALS CONSOLIDATED:
-- - COS-379 (Qubit): Quantum Probability Foundations — confidence_phase, hilbert_projection
-- - COS-381 (Superpose): Belief Superposition — density_matrix, coherence_score, is_superposed
-- - COS-384 (Decohere): Decoherence & Memory Degradation — coherence_syndrome, decoherence_rate, recovery_candidates table
-- - COS-382 (Entangle): Multi-Agent Belief Entanglement — agent_entanglement, agent_ghz_groups, belief_collapse_events tables
--
-- BACKWARD COMPATIBILITY: All changes are additive. Existing data preserved with sensible defaults.
-- ATOMICITY: This migration applies all wave 1 changes in a single transaction.
-- ============================================================================

BEGIN TRANSACTION;

-- ============================================================================
-- PHASE 1: Add columns to memories table
-- (COS-379: Quantum Probability Foundations, COS-384: Decoherence)
-- ============================================================================

-- COS-379: Quantum phase for interference effects
-- The full amplitude: α_i = √(confidence) × exp(i × confidence_phase)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS confidence_phase REAL DEFAULT 0.0;

-- COS-379: Optional pre-computed projection onto common subspace (Hilbert projection)
-- For performance optimization in large-scale interference calculations
ALTER TABLE memories ADD COLUMN IF NOT EXISTS hilbert_projection BYTEA DEFAULT NULL;

-- COS-384: Decoherence diagnostics (JSON: error syndrome, pointer states, etc.)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS coherence_syndrome TEXT DEFAULT NULL;

-- COS-384: Per-memory decoherence rate (λ_eff in quantum formalism)
-- Represents how quickly this memory loses quantum coherence with environment
-- Range: [0.0, 1.0]; higher = faster decay. Depends on temporal_class + access patterns
ALTER TABLE memories ADD COLUMN IF NOT EXISTS decoherence_rate REAL DEFAULT NULL;

-- ============================================================================
-- PHASE 2: Add columns to agent_beliefs table
-- (COS-381: Belief Superposition, COS-382: Entanglement)
-- ============================================================================

-- COS-381: Whether this belief is in quantum superposition (vs. classical mixture)
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS is_superposed INTEGER DEFAULT 0;

-- COS-381: Full belief density matrix (Hermitian positive semidefinite operator)
-- BLOB format: binary serialized format (JSON or msgpack)
-- Structure: 2D array (or sparse format for large Hilbert spaces)
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS belief_density_matrix BYTEA DEFAULT NULL;

-- COS-381: Coherence score — magnitude of off-diagonal terms in density matrix
-- Range: [0.0, 1.0]
-- 0.0 = classical mixture (no superposition), 1.0 = pure state (maximal superposition)
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS coherence_score REAL DEFAULT 0.0;

-- COS-382: JSON array of memory IDs that contributed to belief entanglement
-- Enables tracing which shared memories correlate multiple agents' beliefs
-- Format: JSON array of UUIDs, e.g., '["mem-id-1", "mem-id-2", ...]'
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS entanglement_source_ids TEXT DEFAULT NULL;

-- ============================================================================
-- PHASE 3: Create new tables for decoherence (COS-384)
-- ============================================================================

-- COS-384: recovery_candidates table
-- Records memories that are potentially recoverable from entanglement traces
-- (e.g., syndrome decoding using quantum error correction)
CREATE TABLE IF NOT EXISTS recovery_candidates (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  -- Which memory this candidate can recover
  recoverable_memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  -- Syndrome pattern (error correction codeword)
  syndrome TEXT NOT NULL,
  -- Confidence that recovery will succeed, based on syndrome analysis
  recovery_probability REAL NOT NULL,
  -- Fidelity: how closely recovered state matches original
  expected_fidelity REAL DEFAULT 0.0,
  -- When recovery was last attempted (NULL = never)
  last_recovery_attempt_at TIMESTAMP DEFAULT NULL,
  -- Whether recovery was successful
  recovery_succeeded BOOLEAN DEFAULT NULL,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recovery_candidates_source ON recovery_candidates(source_memory_id);
CREATE INDEX IF NOT EXISTS idx_recovery_candidates_recoverable ON recovery_candidates(recoverable_memory_id);
CREATE INDEX IF NOT EXISTS idx_recovery_candidates_probability ON recovery_candidates(recovery_probability DESC);

-- ============================================================================
-- PHASE 4: Create new tables for entanglement (COS-382)
-- ============================================================================

-- COS-382: agent_entanglement table
-- Pairwise entanglement scores between agents
CREATE TABLE IF NOT EXISTS agent_entanglement (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id_a UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  agent_id_b UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  -- Entanglement entropy S(ρ_A) of Agent A conditioned on Agent B
  -- Measures how much Agent B's state reduces Agent A's uncertainty
  entanglement_entropy REAL NOT NULL,
  -- von Neumann entropy of reduced density matrix
  reduced_entropy_a REAL DEFAULT 0.0,
  reduced_entropy_b REAL DEFAULT 0.0,
  -- Number of shared memories (primary source of entanglement)
  shared_memory_count INT DEFAULT 0,
  -- Average confidence of shared memories
  avg_shared_confidence REAL DEFAULT 0.0,
  -- Bell inequality test result (violation indicates true entanglement)
  -- NULL = not tested, 0.0..2.0 = classical, 2.0..2.828 = quantum
  bell_inequality_chsh REAL DEFAULT NULL,
  -- Timestamp of last entanglement measurement
  measured_at TIMESTAMP DEFAULT NOW(),
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  -- Ensure a < b to avoid duplicate edges
  CONSTRAINT agent_pair_order CHECK (agent_id_a < agent_id_b)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_entanglement_pair ON agent_entanglement(agent_id_a, agent_id_b);
CREATE INDEX IF NOT EXISTS idx_agent_entanglement_entropy ON agent_entanglement(entanglement_entropy DESC);

-- COS-382: agent_ghz_groups table
-- Multi-party entanglement groups (Greenberger-Horne-Zeilinger states)
-- For N ≥ 3 agents sharing a high-confidence memory
CREATE TABLE IF NOT EXISTS agent_ghz_groups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Ordered list of agent IDs in the group (JSON array for efficiency)
  agent_ids TEXT NOT NULL, -- JSON array: '["agent-id-1", "agent-id-2", ...]'
  -- The high-recall memory that entangles this group
  entangling_memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  -- Number of agents in group
  group_size INT NOT NULL,
  -- GHZ violation metric (indicates non-classical correlation strength)
  ghz_violation_metric REAL DEFAULT NULL,
  -- Collective coherence: geometric mean of pairwise entanglement entropies
  collective_coherence REAL DEFAULT 0.0,
  -- Last measurement timestamp
  measured_at TIMESTAMP DEFAULT NOW(),
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_ghz_groups_memory ON agent_ghz_groups(entangling_memory_id);
CREATE INDEX IF NOT EXISTS idx_agent_ghz_groups_size ON agent_ghz_groups(group_size);

-- COS-382: belief_collapse_events table
-- Records when a belief superposition collapses (measurement events)
CREATE TABLE IF NOT EXISTS belief_collapse_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  belief_id UUID NOT NULL REFERENCES agent_beliefs(id) ON DELETE CASCADE,
  agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  -- Which basis state the superposition collapsed to
  collapsed_state TEXT NOT NULL,
  -- Measured probability amplitude of the collapsed state
  measured_amplitude REAL NOT NULL,
  -- Type of measurement: query (explicit), action (implicit), update (information gain)
  collapse_type VARCHAR(50) NOT NULL, -- 'query' | 'action' | 'update'
  -- Context: what triggered the collapse (query terms, decision made, etc.)
  collapse_context TEXT DEFAULT NULL,
  -- Fidelity: how well the collapse matched the expected distribution
  collapse_fidelity REAL DEFAULT 1.0,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_belief_collapse_events_belief ON belief_collapse_events(belief_id);
CREATE INDEX IF NOT EXISTS idx_belief_collapse_events_agent ON belief_collapse_events(agent_id);
CREATE INDEX IF NOT EXISTS idx_belief_collapse_events_type ON belief_collapse_events(collapse_type);

-- ============================================================================
-- PHASE 5: Add indexes for quantum operations
-- ============================================================================

-- Memory table indexes
CREATE INDEX IF NOT EXISTS idx_memories_confidence_phase ON memories(agent_id, confidence_phase)
  WHERE confidence_phase != 0.0;
CREATE INDEX IF NOT EXISTS idx_memories_decoherence_rate ON memories(decoherence_rate DESC)
  WHERE decoherence_rate IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_coherence_syndrome ON memories(agent_id)
  WHERE coherence_syndrome IS NOT NULL;

-- Agent beliefs table indexes
CREATE INDEX IF NOT EXISTS idx_agent_beliefs_superposed ON agent_beliefs(agent_id, is_superposed)
  WHERE is_superposed = 1;
CREATE INDEX IF NOT EXISTS idx_agent_beliefs_coherence ON agent_beliefs(agent_id, coherence_score DESC)
  WHERE is_superposed = 1;
CREATE INDEX IF NOT EXISTS idx_agent_beliefs_entanglement_sources ON agent_beliefs(agent_id)
  WHERE entanglement_source_ids IS NOT NULL;

-- ============================================================================
-- PHASE 6: Create helper functions for quantum operations
-- ============================================================================

-- Function: Initialize confidence phase from existing confidence values
-- Maps classical confidence to quantum amplitude (phase = 0 for positive)
CREATE OR REPLACE FUNCTION initialize_quantum_amplitudes()
RETURNS TABLE (updated_count INT) AS $$
DECLARE
  v_count INT;
BEGIN
  UPDATE memories
  SET confidence_phase = 0.0
  WHERE confidence_phase IS NULL OR confidence_phase = 0.0;

  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN QUERY SELECT v_count::INT;
END;
$$ LANGUAGE plpgsql;

-- Function: Compute entanglement entropy between two agents
-- Based on shared memory access patterns
CREATE OR REPLACE FUNCTION compute_entanglement_entropy(
  p_agent_a UUID,
  p_agent_b UUID
) RETURNS REAL AS $$
DECLARE
  v_entropy REAL;
  v_total_edges INT;
  v_shared_edges INT;
BEGIN
  -- Count total edges per agent
  SELECT COUNT(*) INTO v_total_edges
  FROM knowledge_edges
  WHERE agent_id IN (p_agent_a, p_agent_b)
  GROUP BY agent_id
  LIMIT 1;

  -- Count shared memory edges
  SELECT COUNT(DISTINCT source_id) INTO v_shared_edges
  FROM knowledge_edges ke1
  JOIN knowledge_edges ke2 ON ke1.source_id = ke2.source_id
  WHERE ke1.agent_id = p_agent_a AND ke2.agent_id = p_agent_b;

  -- Entropy proxy: shared edges / total edges
  -- (More rigorous: von Neumann entropy of reduced density matrix)
  IF v_total_edges = 0 THEN
    RETURN 0.0;
  END IF;

  v_entropy := (v_shared_edges::REAL / v_total_edges::REAL);
  RETURN v_entropy;
END;
$$ LANGUAGE plpgsql;

-- Function: Detect GHZ-type multi-party entanglement
-- Returns list of agent groups sharing high-recall memory
CREATE OR REPLACE FUNCTION detect_ghz_groups(
  p_min_group_size INT DEFAULT 3,
  p_min_memory_recall INT DEFAULT 50
) RETURNS TABLE (agent_ids TEXT, memory_id UUID, group_size INT) AS $$
BEGIN
  RETURN QUERY
  WITH memory_access AS (
    SELECT
      source_id AS memory_id,
      ARRAY_AGG(DISTINCT agent_id ORDER BY agent_id) AS agents,
      COUNT(DISTINCT agent_id) AS agent_count
    FROM knowledge_edges
    WHERE source_table = 'memories'
    GROUP BY source_id
  ),
  high_recall_memories AS (
    SELECT m.id, array_to_string(ma.agents, ', ') AS agent_ids, ma.agent_count
    FROM memory_access ma
    JOIN memories m ON m.id = ma.memory_id
    WHERE m.recalled_count >= p_min_memory_recall
      AND ma.agent_count >= p_min_group_size
    ORDER BY m.recalled_count DESC
  )
  SELECT agent_ids, id, agent_count FROM high_recall_memories;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- PHASE 7: Create views for quantum analysis
-- ============================================================================

-- View: Superposed beliefs (unresolved agent beliefs in quantum superposition)
CREATE OR REPLACE VIEW superposed_beliefs AS
SELECT
  ab.id,
  ab.agent_id,
  ab.query_key,
  ab.is_superposed,
  ab.coherence_score,
  ab.entanglement_source_ids,
  ab.created_at,
  ab.updated_at
FROM agent_beliefs ab
WHERE ab.is_superposed = 1;

-- View: Entangled agent pairs (agents sharing belief states)
CREATE OR REPLACE VIEW entangled_agent_pairs AS
SELECT
  ae.agent_id_a,
  ae.agent_id_b,
  ae.entanglement_entropy,
  ae.bell_inequality_chsh,
  ae.shared_memory_count,
  ae.measured_at
FROM agent_entanglement ae
ORDER BY ae.entanglement_entropy DESC;

-- View: Memories with decoherence diagnostics
CREATE OR REPLACE VIEW decoherent_memories AS
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
ORDER BY decoherence_rate DESC NULLS LAST;

-- View: Recent belief collapse events (measurement outcomes)
CREATE OR REPLACE VIEW recent_belief_collapses AS
SELECT
  bce.id,
  bce.agent_id,
  bce.belief_id,
  bce.collapsed_state,
  bce.collapse_type,
  bce.collapse_fidelity,
  bce.created_at
FROM belief_collapse_events bce
WHERE bce.created_at > NOW() - INTERVAL '7 days'
ORDER BY bce.created_at DESC;

-- ============================================================================
-- PHASE 8: Data population and validation
-- ============================================================================

-- Initialize quantum phase for all existing memories
-- Phase is 0 (positive amplitude) by default for backward compatibility
SELECT initialize_quantum_amplitudes();

-- Compute initial entanglement scores for agent pairs
-- This populates agent_entanglement based on current shared memory patterns
WITH agent_pairs AS (
  SELECT DISTINCT
    (SELECT id FROM agents WHERE id < b.id) AS agent_a,
    b.id AS agent_b
  FROM agents a, agents b
  WHERE a.id < b.id
)
INSERT INTO agent_entanglement (agent_id_a, agent_id_b, entanglement_entropy)
SELECT
  ap.agent_a,
  ap.agent_b,
  compute_entanglement_entropy(ap.agent_a, ap.agent_b)
FROM agent_pairs ap
WHERE ap.agent_a IS NOT NULL AND ap.agent_b IS NOT NULL
ON CONFLICT (agent_id_a, agent_id_b) DO UPDATE
SET
  entanglement_entropy = EXCLUDED.entanglement_entropy,
  updated_at = NOW();

-- Detect initial GHZ groups
INSERT INTO agent_ghz_groups (agent_ids, entangling_memory_id, group_size)
SELECT
  agent_ids,
  memory_id,
  group_size
FROM detect_ghz_groups(3, 50)
ON CONFLICT DO NOTHING;

-- ============================================================================
-- PHASE 9: Rollback plan (reverse migration)
-- ============================================================================

/*
ROLLBACK PROCEDURE (if needed):

-- Drop new tables (in reverse order of creation)
DROP TABLE IF EXISTS belief_collapse_events CASCADE;
DROP TABLE IF EXISTS agent_ghz_groups CASCADE;
DROP TABLE IF EXISTS agent_entanglement CASCADE;
DROP TABLE IF EXISTS recovery_candidates CASCADE;

-- Drop views
DROP VIEW IF EXISTS recent_belief_collapses CASCADE;
DROP VIEW IF EXISTS decoherent_memories CASCADE;
DROP VIEW IF EXISTS entangled_agent_pairs CASCADE;
DROP VIEW IF EXISTS superposed_beliefs CASCADE;

-- Drop functions
DROP FUNCTION IF EXISTS detect_ghz_groups(INT, INT) CASCADE;
DROP FUNCTION IF EXISTS compute_entanglement_entropy(UUID, UUID) CASCADE;
DROP FUNCTION IF EXISTS initialize_quantum_amplitudes() CASCADE;

-- Remove columns from agent_beliefs
ALTER TABLE agent_beliefs DROP COLUMN IF EXISTS entanglement_source_ids CASCADE;
ALTER TABLE agent_beliefs DROP COLUMN IF EXISTS coherence_score CASCADE;
ALTER TABLE agent_beliefs DROP COLUMN IF EXISTS belief_density_matrix CASCADE;
ALTER TABLE agent_beliefs DROP COLUMN IF EXISTS is_superposed CASCADE;

-- Remove columns from memories
ALTER TABLE memories DROP COLUMN IF EXISTS decoherence_rate CASCADE;
ALTER TABLE memories DROP COLUMN IF EXISTS coherence_syndrome CASCADE;
ALTER TABLE memories DROP COLUMN IF EXISTS hilbert_projection CASCADE;
ALTER TABLE memories DROP COLUMN IF EXISTS confidence_phase CASCADE;

-- Verify: SELECT COUNT(*) FROM agent_beliefs; -- should match pre-migration count
-- Verify: SELECT COUNT(*) FROM memories; -- should match pre-migration count
*/

-- ============================================================================
-- PHASE 10: Conflict resolution summary and migration notes
-- ============================================================================

/*
CONFLICT ANALYSIS:

1. Column naming:
   - No conflicts. All new columns have distinct names across tables.
   - agent_beliefs.coherence_score (COS-381) vs memories.coherence_syndrome (COS-384): different meanings, separate tables ✓

2. Type consistency:
   - Density matrices (COS-381) stored as BYTEA (binary blob) for performance
   - Coherence syndrome (COS-384) stored as TEXT (JSON for human readability)
   - This allows different serialization formats suitable to use case ✓

3. Foreign key integrity:
   - recovery_candidates.source_memory_id → memories.id ✓
   - recovery_candidates.recoverable_memory_id → memories.id ✓
   - agent_entanglement.agent_id_a → agents.id ✓
   - agent_entanglement.agent_id_b → agents.id ✓
   - agent_ghz_groups.entangling_memory_id → memories.id ✓
   - belief_collapse_events.belief_id → agent_beliefs.id ✓
   - belief_collapse_events.agent_id → agents.id ✓

4. Migration dependency order:
   - Phase 1-2: Add columns (no dependencies, can run in parallel)
   - Phase 3-4: Create tables (depends on memories, agents, agent_beliefs existing)
   - Phase 5: Add indexes (depends on columns/tables existing)
   - Phase 6: Create functions (no dependencies)
   - Phase 7: Create views (depends on tables existing)
   - Phase 8: Data population (depends on all above)

5. Backward compatibility:
   - All new columns have DEFAULT values (0, 0.0, NULL, false)
   - Existing beliefs/memories remain classical by default
   - No existing data will be modified by this migration
   - New quantum features are opt-in
   ✓

PERFORMANCE IMPACT (estimated):

Current scale (122 memories, 26 agents):
- New columns: ~4 KB per memory (phase, syndrome) + ~20 KB per belief (density matrix)
  Total overhead: ~500 KB for existing data
- New tables: recovery_candidates (sparse, ~100-500 rows), agent_entanglement (sparse, ~325 rows max),
  agent_ghz_groups (sparse, ~10-50 rows), belief_collapse_events (event log, unbounded)
  Total initial: ~200 KB

Scaling to 10× (1,220 memories, 260 agents):
- Column data: ~5 MB
- Entanglement table: ~13,000 rows (dense pairwise graph), ~5 MB
- Recovery candidates: ~1,000 rows, ~1 MB
- GHZ groups: ~100 rows, ~50 KB
- Collapse events: ~10K events over 30 days, ~2 MB
  Total estimated: ~25-30 MB

Mitigation:
- Indexes on frequently-filtered columns (agent_id, superposition status, entropy scores)
- Partitioning collapse events by created_at for large deployments
- Sparse storage for BLOBs (only materialized when needed)

TESTING PROCEDURE:

1. Run on staging database
2. Verify all columns exist with correct types:
   SELECT column_name, data_type FROM information_schema.columns
   WHERE table_name IN ('memories', 'agent_beliefs') AND column_name LIKE '%quantum%' OR column_name LIKE '%entangle%';
3. Verify all tables created:
   SELECT table_name FROM information_schema.tables
   WHERE table_name IN ('recovery_candidates', 'agent_entanglement', 'agent_ghz_groups', 'belief_collapse_events');
4. Verify indexes exist:
   SELECT indexname FROM pg_indexes
   WHERE tablename IN ('memories', 'agent_beliefs', 'recovery_candidates', 'agent_entanglement', 'agent_ghz_groups');
5. Verify views created:
   SELECT viewname FROM pg_views WHERE schemaname = 'public'
   AND viewname IN ('superposed_beliefs', 'entangled_agent_pairs', 'decoherent_memories', 'recent_belief_collapses');
6. Test helper functions:
   SELECT initialize_quantum_amplitudes();
   SELECT compute_entanglement_entropy(agent_id_a, agent_id_b) FROM agent_entanglement LIMIT 1;
   SELECT * FROM detect_ghz_groups(3, 50) LIMIT 5;
7. Validate no data corruption:
   SELECT COUNT(*) FROM agent_beliefs; -- should equal pre-migration count
   SELECT COUNT(*) FROM memories; -- should equal pre-migration count
8. Spot-check quantum data:
   SELECT COUNT(*) FROM superposed_beliefs; -- should be 0 initially
   SELECT COUNT(*) FROM entangled_agent_pairs WHERE entanglement_entropy > 0.1; -- likely 0-10 pairs
*/

COMMIT;
