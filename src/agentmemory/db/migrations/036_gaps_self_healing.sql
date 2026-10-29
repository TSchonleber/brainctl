-- 036_gaps_self_healing.sql
--
-- Extend the knowledge_gaps CHECK constraint to cover three new self-healing
-- scan types that run during the nightly consolidation cycle:
--
--   orphan_memory         — a memory with zero knowledge_edges and zero
--                           recalls in the last N days; candidate for
--                           retirement or compression.
--   broken_edge           — a knowledge_edges row whose source_id or
--                           target_id no longer exists (soft-deleted row
--                           never cleaned up, or a rogue INSERT).
--   unreferenced_entity   — an entity with no incoming edges, no outgoing
--                           edges, no linked memories, no linked events,
--                           and no recall activity — never mentioned by
--                           anyone.
--
-- SQLite can't ALTER a CHECK constraint in place, so we rebuild the table:
--   1. create knowledge_gaps_new with the expanded constraint
--   2. copy everything over
--   3. drop the old table
--   4. rename
--   5. re-create indexes

PRAGMA foreign_keys = OFF;

-- Latent-bug fix piggy-backed on this migration: the `recent_belief_collapses`
-- view in init_schema.sql references a non-existent table
-- `belief_collapse_events_old`, a leftover from a partial rename. SQLite
-- tolerates this until the next DDL operation triggers a full schema check
-- — at which point the DROP TABLE below would fail. Rebuild the view to
-- point at `belief_collapse_events` (the real table) so the rest of this
-- migration — and any future ALTER that touches schema — can proceed.
--
-- Indentation here must match init_schema.sql byte-for-byte so the
-- schema-parity test stays green.
DROP VIEW IF EXISTS recent_belief_collapses;
CREATE VIEW recent_belief_collapses AS
            SELECT bce.id, bce.agent_id, bce.belief_id, bce.collapsed_state,
                   bce.collapse_type, bce.collapse_fidelity, bce.created_at
            FROM belief_collapse_events bce
            WHERE bce.created_at > datetime('now', '-7 days')
            ORDER BY bce.created_at DESC;

-- Rebuild knowledge_gaps with the expanded CHECK constraint.
-- Order matters: stash rows into a TEMP table, drop the old table, then
-- CREATE the new one *under the original name* (avoids the "quoted name"
-- sqlite_master entry that ALTER TABLE ... RENAME produces — the parity
-- test compares sqlite_master text byte-for-byte).
CREATE TEMP TABLE knowledge_gaps_backup AS
    SELECT id, gap_type, scope, detected_at, triggered_by, severity, resolved_at, resolution_note
    FROM knowledge_gaps;

DROP TABLE knowledge_gaps;

CREATE TABLE knowledge_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_type TEXT NOT NULL CHECK(gap_type IN (
        'coverage_hole',         -- no memories in scope at all
        'staleness_hole',        -- memories exist but all too old
        'confidence_hole',       -- memories exist but avg confidence too low
        'contradiction_hole',    -- memories contradict each other
        -- Migration 036 self-healing scan types
        'orphan_memory',         -- memory with no edges + no recalls + old
        'broken_edge',           -- knowledge_edges row points at deleted row
        'unreferenced_entity'    -- entity with nothing linking to it
    )),
    scope TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    triggered_by TEXT,                          -- query or scan that revealed the gap
    severity REAL NOT NULL DEFAULT 0.5          -- 0.0–1.0
        CHECK(severity >= 0.0 AND severity <= 1.0),
    resolved_at TEXT,
    resolution_note TEXT
);

INSERT INTO knowledge_gaps
    (id, gap_type, scope, detected_at, triggered_by, severity, resolved_at, resolution_note)
SELECT id, gap_type, scope, detected_at, triggered_by, severity, resolved_at, resolution_note
FROM knowledge_gaps_backup;

DROP TABLE knowledge_gaps_backup;

-- Re-create indexes. Formatting must match init_schema.sql so sqlite_master
-- entries are byte-identical between fresh and upgraded paths.
CREATE INDEX idx_gaps_scope ON knowledge_gaps(scope);
CREATE INDEX idx_gaps_type ON knowledge_gaps(gap_type);
CREATE INDEX idx_gaps_unresolved ON knowledge_gaps(resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX idx_gaps_severity ON knowledge_gaps(severity DESC) WHERE resolved_at IS NULL;

PRAGMA foreign_keys = ON;
