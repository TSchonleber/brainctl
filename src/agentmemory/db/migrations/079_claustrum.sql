-- Migration 079: claustrum — Phase 1 cross-modal binding
--
-- Avenue 3 from research/autonomous-research-avenues-2026-05-20.md.
-- The claustrum is a thin sheet that everyone projects to and that
-- projects to everyone (Crick & Koch 2005). Function: cross-modal
-- binding / consciousness integration.
--
-- brainctl analog: detect when multiple retrieval modalities (FTS,
-- vector, hybrid_rrf, pagerank_boost, multi_pass, temporal_expand,
-- entorhinal_grid, procedural_search) converge on the same memory.
-- Cross-modal convergence is a strong signal that wasn't previously
-- tracked.
--
-- Phase 1 ships:
--   claustrum_binding_events — when ≥2 modalities surface the same
--                              memory_id within a window
--   claustrum_modality_catalog — known retrieval modalities + meta
--   claustrum_state — running stats
--
-- Phase 2 auto-detects from cmd_search. Phase 3 boosts memory
-- confidence by binding_strength when multiple modalities agree.
--
-- Rollback:
--   DROP TABLE IF EXISTS claustrum_binding_events;
--   DROP TABLE IF EXISTS claustrum_modality_catalog;
--   DROP TABLE IF EXISTS claustrum_state;
--   DELETE FROM schema_version WHERE version = 79;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS claustrum_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    binding_window_seconds INTEGER NOT NULL DEFAULT 60 CHECK(binding_window_seconds > 0),
    min_modalities_for_binding INTEGER NOT NULL DEFAULT 2 CHECK(min_modalities_for_binding >= 2),
    total_bindings INTEGER NOT NULL DEFAULT 0,
    enforcement_mode TEXT NOT NULL DEFAULT 'shadow' CHECK(enforcement_mode IN ('shadow', 'enforce', 'disabled')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO claustrum_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS claustrum_modality_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    weight REAL NOT NULL DEFAULT 1.0 CHECK(weight BETWEEN 0.0 AND 1.0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

INSERT OR IGNORE INTO claustrum_modality_catalog (name, description, weight) VALUES
    ('fts', 'BM25 full-text search via memories_fts', 1.0),
    ('vector', 'cosine-distance search via vec_memories', 1.0),
    ('hybrid_rrf', 'reciprocal rank fusion of FTS + vector', 1.0),
    ('pagerank_boost', 'SR-style retrieval (PageRank == Successor Representation)', 0.8),
    ('multi_pass', 'SDM-style iterative convergence', 0.8),
    ('temporal_expand', 'TCM temporal contiguity expansion', 0.7),
    ('entorhinal_grid', 'grid-cell hash activation lookup', 0.9),
    ('procedural_search', 'procedural memory FTS5 search', 0.9),
    ('ca3_completion', 'CA3 pattern-completion via hippocampus_ca3', 1.0);

CREATE TABLE IF NOT EXISTS claustrum_binding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bound_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    memory_id INTEGER NOT NULL,
    agent_id TEXT,
    query_hash TEXT,
    modalities TEXT NOT NULL,       -- comma-separated list of modality names that converged
    modality_count INTEGER NOT NULL CHECK(modality_count >= 2),
    binding_strength REAL NOT NULL CHECK(binding_strength BETWEEN 0.0 AND 1.0),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_cbe_recent ON claustrum_binding_events(bound_at);
CREATE INDEX IF NOT EXISTS idx_cbe_memory ON claustrum_binding_events(memory_id, bound_at);
CREATE INDEX IF NOT EXISTS idx_cbe_strength ON claustrum_binding_events(binding_strength);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (79, 'claustrum Phase 1: cross-modal binding (3 tables, 9 modality catalog)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
