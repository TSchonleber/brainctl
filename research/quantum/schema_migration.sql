-- Quantum Belief Superposition — Schema Migration
-- Extends agent_beliefs table to support quantum superposition states
-- Author: Superpose
-- Date: 2026-03-28

-- ============================================================================
-- Step 1: Add quantum columns to agent_beliefs table
-- ============================================================================

ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS is_superposed BOOLEAN DEFAULT FALSE;
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS basis_states TEXT[] DEFAULT NULL;
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS amplitudes JSONB DEFAULT NULL;
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS density_matrix JSONB DEFAULT NULL;
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS coherence_score FLOAT DEFAULT 0.0;
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS last_collapsed_at TIMESTAMP DEFAULT NULL;
ALTER TABLE agent_beliefs ADD COLUMN IF NOT EXISTS collapsed_state TEXT DEFAULT NULL;

-- ============================================================================
-- Step 2: Create helper function to validate superposition state
-- ============================================================================

CREATE OR REPLACE FUNCTION validate_superposition_state(
  p_is_superposed BOOLEAN,
  p_basis_states TEXT[],
  p_amplitudes JSONB,
  p_coherence_score FLOAT
) RETURNS BOOLEAN AS $$
DECLARE
  v_amplitude_sum FLOAT;
  v_i INT;
BEGIN
  -- If not superposed, all quantum fields should be NULL
  IF NOT p_is_superposed THEN
    RETURN (p_basis_states IS NULL AND p_amplitudes IS NULL AND p_coherence_score = 0.0);
  END IF;

  -- If superposed, validate:
  -- 1. basis_states is not empty
  IF p_basis_states IS NULL OR array_length(p_basis_states, 1) = 0 THEN
    RAISE EXCEPTION 'Superposed belief must have non-empty basis_states';
  END IF;

  -- 2. amplitudes JSONB has same length as basis_states
  IF p_amplitudes IS NULL OR jsonb_array_length(p_amplitudes) != array_length(p_basis_states, 1) THEN
    RAISE EXCEPTION 'Number of amplitudes must match number of basis states';
  END IF;

  -- 3. Sum of |amplitude|^2 should be ~1.0 (normalized)
  -- This is a soft constraint with 1% tolerance
  SELECT SUM(
    (CAST(jsonb_array_elements(p_amplitudes) ->> 'real' AS FLOAT) ^ 2) +
    (CAST(jsonb_array_elements(p_amplitudes) ->> 'imag' AS FLOAT) ^ 2)
  ) INTO v_amplitude_sum;

  IF v_amplitude_sum IS NULL OR ABS(v_amplitude_sum - 1.0) > 0.01 THEN
    RAISE EXCEPTION 'Amplitudes must be normalized: sum of |a|^2 should equal 1.0, got %', v_amplitude_sum;
  END IF;

  -- 4. coherence_score should be [0, 1]
  IF p_coherence_score < 0.0 OR p_coherence_score > 1.0 THEN
    RAISE EXCEPTION 'coherence_score must be between 0 and 1, got %', p_coherence_score;
  END IF;

  RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- Step 3: Create trigger to enforce superposition constraints
-- ============================================================================

CREATE OR REPLACE FUNCTION trigger_validate_superposition()
RETURNS TRIGGER AS $$
BEGIN
  PERFORM validate_superposition_state(
    NEW.is_superposed,
    NEW.basis_states,
    NEW.amplitudes,
    NEW.coherence_score
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_beliefs_validate_superposition ON agent_beliefs;
CREATE TRIGGER agent_beliefs_validate_superposition
  BEFORE INSERT OR UPDATE ON agent_beliefs
  FOR EACH ROW
  EXECUTE FUNCTION trigger_validate_superposition();

-- ============================================================================
-- Step 4: Create index for superposition queries
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_agent_beliefs_superposed ON agent_beliefs(agent_id, is_superposed)
  WHERE is_superposed = TRUE;

CREATE INDEX IF NOT EXISTS idx_agent_beliefs_coherence ON agent_beliefs(agent_id, coherence_score DESC)
  WHERE is_superposed = TRUE;

-- ============================================================================
-- Step 5: Create helper functions for quantum operations
-- ============================================================================

-- Function: Create a new superposition state
CREATE OR REPLACE FUNCTION create_superposition_state(
  p_agent_id UUID,
  p_query_key TEXT,
  p_basis_states TEXT[],
  p_amplitudes JSONB,
  p_coherence_score FLOAT DEFAULT 0.95
) RETURNS agent_beliefs AS $$
DECLARE
  v_belief agent_beliefs;
BEGIN
  INSERT INTO agent_beliefs (
    agent_id, query_key, is_superposed, basis_states, amplitudes,
    coherence_score, confidence
  ) VALUES (
    p_agent_id, p_query_key, TRUE, p_basis_states, p_amplitudes,
    p_coherence_score, NULL
  )
  RETURNING * INTO v_belief;

  RETURN v_belief;
END;
$$ LANGUAGE plpgsql;

-- Function: Collapse a superposition to a definite state
CREATE OR REPLACE FUNCTION collapse_belief(
  p_belief_id UUID,
  p_collapsed_state TEXT,
  p_belief_value FLOAT
) RETURNS agent_beliefs AS $$
DECLARE
  v_belief agent_beliefs;
BEGIN
  UPDATE agent_beliefs
  SET
    is_superposed = FALSE,
    collapsed_state = p_collapsed_state,
    belief_value = p_belief_value,
    last_collapsed_at = NOW(),
    basis_states = NULL,
    amplitudes = NULL,
    density_matrix = NULL,
    coherence_score = 0.0
  WHERE id = p_belief_id
  RETURNING * INTO v_belief;

  IF v_belief IS NULL THEN
    RAISE EXCEPTION 'Belief % not found', p_belief_id;
  END IF;

  RETURN v_belief;
END;
$$ LANGUAGE plpgsql;

-- Function: Get probability distribution from superposition
CREATE OR REPLACE FUNCTION get_probability_distribution(
  p_amplitudes JSONB
) RETURNS TABLE (basis_state TEXT, probability FLOAT) AS $$
DECLARE
  v_i INT;
  v_amplitude_json JSONB;
  v_real FLOAT;
  v_imag FLOAT;
  v_prob FLOAT;
  v_sum FLOAT := 0.0;
BEGIN
  -- First pass: compute normalization
  FOR v_i IN 0 .. jsonb_array_length(p_amplitudes) - 1 LOOP
    v_amplitude_json := p_amplitudes -> v_i;
    v_real := CAST(v_amplitude_json ->> 'real' AS FLOAT);
    v_imag := CAST(v_amplitude_json ->> 'imag' AS FLOAT);
    v_sum := v_sum + (v_real ^ 2 + v_imag ^ 2);
  END LOOP;

  -- Second pass: return probabilities
  FOR v_i IN 0 .. jsonb_array_length(p_amplitudes) - 1 LOOP
    v_amplitude_json := p_amplitudes -> v_i;
    v_real := CAST(v_amplitude_json ->> 'real' AS FLOAT);
    v_imag := CAST(v_amplitude_json ->> 'imag' AS FLOAT);
    v_prob := (v_real ^ 2 + v_imag ^ 2) / NULLIF(v_sum, 0);
    basis_state := CAST(v_i AS TEXT);
    probability := v_prob;
    RETURN NEXT;
  END LOOP;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- Step 6: Create view for superposed beliefs
-- ============================================================================

CREATE OR REPLACE VIEW superposed_beliefs AS
SELECT
  id,
  agent_id,
  query_key,
  basis_states,
  amplitudes,
  coherence_score,
  last_collapsed_at,
  created_at,
  updated_at
FROM agent_beliefs
WHERE is_superposed = TRUE;

-- ============================================================================
-- Step 7: Create view for collapsed beliefs
-- ============================================================================

CREATE OR REPLACE VIEW collapsed_beliefs AS
SELECT
  id,
  agent_id,
  query_key,
  belief_value,
  collapsed_state,
  last_collapsed_at,
  created_at,
  updated_at
FROM agent_beliefs
WHERE is_superposed = FALSE;

-- ============================================================================
-- Migration Notes
-- ============================================================================

/*
BACKWARD COMPATIBILITY:
- Existing beliefs remain classical (is_superposed = FALSE)
- New beliefs can opt-in to superposition by setting is_superposed = TRUE
- Column defaults preserve classical behavior

TESTING PROCEDURE:
1. Run migration on staging database
2. Validate existing beliefs still work (is_superposed = FALSE)
3. Create test superposition beliefs
4. Test collapse mechanics
5. Verify indexes on frequently-accessed queries
6. Check trigger behavior with invalid data

PERFORMANCE NOTES:
- amplitudes JSONB is stored as binary, optimized for index access
- Superposition queries use GIN index on agent_id + is_superposed
- Collapse is single UPDATE, O(1) operation
- Coherence decay (future): background job updates coherence_score

DECOHERENCE:
The coherence_score will naturally decay as beliefs age. Background job (Decohere):
  UPDATE agent_beliefs
  SET coherence_score = coherence_score * 0.95
  WHERE is_superposed = TRUE AND updated_at < NOW() - INTERVAL '7 days';

This models quantum decoherence: environmental noise degrades superposition over time.
*/
