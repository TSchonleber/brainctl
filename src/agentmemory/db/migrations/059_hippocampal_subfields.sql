-- Migration 059: hippocampal subfields — DG / CA3 / CA1 split
--
-- brainctl's existing hippocampus.py treats the hippocampus as a flat
-- abstraction (one consolidation pipeline). Biology splits it into:
--   DG  (dentate gyrus)  — PATTERN SEPARATION: orthogonalizes inputs
--                          so similar contexts get distinct neural codes
--                          before they hit the storage substrate.
--   CA3 (CA3 region)     — PATTERN COMPLETION: given a partial cue,
--                          completes via recurrent collaterals to the
--                          full stored pattern.
--   CA1 (CA1 region)     — OUTPUT + comparison: routes the completed
--                          pattern to entorhinal cortex and on to cortex.
--
-- brainctl today is implicitly CA3 (similarity-based recall) without an
-- explicit DG step at write time. That means near-duplicate memories
-- pattern-complete onto each other rather than being stored as distinct
-- traces — a known source of retrieval interference under W(m).
--
-- This phase adds:
--   hippocampus_pattern_separations  — DG audit table: every write that
--                                       had a near-neighbor in scope gets
--                                       a row recording the separation
--                                       (distance, decision, assigned tag).
--   hippocampus_completion_traces    — CA3 audit table: every retrieval
--                                       that pattern-completed past a
--                                       similarity threshold gets a row
--                                       (cue, completed_to, distance).
--
-- These are AUDIT/LEARNING tables, not new storage. The actual memories
-- live in the existing `memories` table; subfields just annotate.
--
-- Rollback:
--   DROP TABLE IF EXISTS hippocampus_completion_traces;
--   DROP TABLE IF EXISTS hippocampus_pattern_separations;
--   DELETE FROM schema_version WHERE version = 59;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS hippocampus_pattern_separations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER,                    -- new memory being written
    nearest_neighbor_id INTEGER,          -- closest existing memory in scope
    cosine_distance REAL NOT NULL,        -- 0=identical, 1=orthogonal
    decision TEXT NOT NULL CHECK(decision IN ('separate','merge','deduplicate','passthrough')),
    separation_tag TEXT,                  -- when 'separate', tag distinguishing
                                          -- the new code from the neighbor
    scope TEXT,
    agent_id TEXT,
    decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_hs_separations_memory ON hippocampus_pattern_separations(memory_id);
CREATE INDEX IF NOT EXISTS idx_hs_separations_recent ON hippocampus_pattern_separations(decided_at);

CREATE TABLE IF NOT EXISTS hippocampus_completion_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_hash TEXT NOT NULL,             -- hash of the original cue
    completed_to_memory_id INTEGER NOT NULL,
    distance REAL NOT NULL,               -- how far the cue was from the
                                          -- completed pattern
    rank INTEGER NOT NULL DEFAULT 1,      -- 1 = top match, etc.
    agent_id TEXT,
    completed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_hs_completion_recent ON hippocampus_completion_traces(completed_at);
CREATE INDEX IF NOT EXISTS idx_hs_completion_memory ON hippocampus_completion_traces(completed_to_memory_id);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (59, 'hippocampal subfields: DG pattern-separation + CA3 pattern-completion audit',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
