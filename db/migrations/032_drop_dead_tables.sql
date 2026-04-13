-- 032_drop_dead_tables.sql
-- Phase 2a schema cleanup: drop six tables that have zero Python references
-- in src/, bin/, tests/, scripts/, research/, config/, agents/, ui/ or elsewhere.
--
-- Safety net: run `brainctl archive-dead-tables` first to dump any rows to a
-- JSON file before applying this migration.
--
-- Drop order respects foreign keys: views that reference these tables are
-- dropped first, then indexes, then the tables themselves.

-- Drop views that reference dead tables
DROP VIEW IF EXISTS entangled_agent_pairs;

-- Drop indexes tied to dead tables (DROP TABLE usually removes these, but be
-- explicit so the result is identical on databases where any indexes were
-- already missing).
DROP INDEX IF EXISTS idx_experiments_status;
DROP INDEX IF EXISTS idx_experiments_agent;
DROP INDEX IF EXISTS idx_experiments_outcome;
DROP INDEX IF EXISTS idx_assessments_agent;
DROP INDEX IF EXISTS idx_assessments_time;
DROP INDEX IF EXISTS idx_recovery_candidates_source;
DROP INDEX IF EXISTS idx_recovery_candidates_recoverable;
DROP INDEX IF EXISTS idx_recovery_candidates_probability;
DROP INDEX IF EXISTS idx_agent_entanglement_pair;
DROP INDEX IF EXISTS idx_agent_entanglement_entropy;
DROP INDEX IF EXISTS idx_agent_ghz_groups_memory;
DROP INDEX IF EXISTS idx_agent_ghz_groups_size;

-- Drop the dead tables.
-- Order: tables whose FKs point OUT to live tables come first; tables with no
-- inbound FKs from still-live tables can be dropped in any order.
DROP TABLE IF EXISTS agent_ghz_groups;
DROP TABLE IF EXISTS agent_entanglement;
DROP TABLE IF EXISTS recovery_candidates;
DROP TABLE IF EXISTS health_snapshots;
DROP TABLE IF EXISTS self_assessments;
DROP TABLE IF EXISTS cognitive_experiments;
