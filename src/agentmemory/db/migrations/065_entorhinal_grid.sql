-- Migration 065: Entorhinal cortex — conceptual grid indexing
-- brainctl has temporal grid (epochs) but no conceptual grid. Grid cells in
-- EC tile concept-space with periodic basis functions; each memory activates
-- a small subset of cells, giving cheap pattern-indexed lookup.
-- Phase 1: schema + audit. Phase 2: wire into retrieval.

CREATE TABLE IF NOT EXISTS entorhinal_grid_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scale INTEGER NOT NULL,         -- grid scale (1=fine, 2=medium, 3=coarse...)
    cell_index INTEGER NOT NULL,    -- index within the scale
    basis_hash TEXT NOT NULL,       -- hash of the periodic basis function used
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(scale, cell_index)
);

-- Per-memory grid activations: which cells fire for this memory.
CREATE TABLE IF NOT EXISTS entorhinal_memory_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL,
    cell_id INTEGER NOT NULL REFERENCES entorhinal_grid_cells(id) ON DELETE CASCADE,
    activation REAL NOT NULL DEFAULT 1.0,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(memory_id, cell_id)
);
CREATE INDEX IF NOT EXISTS idx_eg_activations_memory ON entorhinal_memory_activations(memory_id);
CREATE INDEX IF NOT EXISTS idx_eg_activations_cell ON entorhinal_memory_activations(cell_id, activation DESC);

-- Seed canonical 3-scale grid cells (16 cells per scale = 48 total).
INSERT OR IGNORE INTO entorhinal_grid_cells (scale, cell_index, basis_hash, description)
SELECT 1, n, 'fine:' || n, 'fine-grained grid cell ' || n FROM (
    SELECT 0 AS n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3
    UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7
    UNION SELECT 8 UNION SELECT 9 UNION SELECT 10 UNION SELECT 11
    UNION SELECT 12 UNION SELECT 13 UNION SELECT 14 UNION SELECT 15);
INSERT OR IGNORE INTO entorhinal_grid_cells (scale, cell_index, basis_hash, description)
SELECT 2, n, 'medium:' || n, 'medium-grained grid cell ' || n FROM (
    SELECT 0 AS n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3
    UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7
    UNION SELECT 8 UNION SELECT 9 UNION SELECT 10 UNION SELECT 11
    UNION SELECT 12 UNION SELECT 13 UNION SELECT 14 UNION SELECT 15);
INSERT OR IGNORE INTO entorhinal_grid_cells (scale, cell_index, basis_hash, description)
SELECT 3, n, 'coarse:' || n, 'coarse-grained grid cell ' || n FROM (
    SELECT 0 AS n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3
    UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7
    UNION SELECT 8 UNION SELECT 9 UNION SELECT 10 UNION SELECT 11
    UNION SELECT 12 UNION SELECT 13 UNION SELECT 14 UNION SELECT 15);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (65, 'Entorhinal cortex: 3-scale conceptual grid (48 cells) for pattern-indexed retrieval', strftime('%Y-%m-%dT%H:%M:%S','now'));
