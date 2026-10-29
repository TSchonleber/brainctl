-- Migration 072: workspace bandwidth limit — Phase 1 schema
--
-- The May 15 brain_region_coverage.md audit flagged the workspace
-- (global neuronal workspace) as partial: "Fixed salience threshold,
-- no org_state coupling, no enforced bandwidth limit (any module can
-- write)."
--
-- The thalamus mode-broadcast layer (shipped via thalamus Phase 2)
-- closed the org_state coupling half. This migration closes the
-- remaining half: a top-K-per-epoch bandwidth limit on workspace
-- broadcasts.
--
-- Biology: the global neuronal workspace (Dehaene-Changeux model) has
-- a hard bandwidth — only ~4 chunks can be "ignited" at a time. Without
-- that constraint, the workspace degenerates into a firehose. brainctl's
-- current workspace_broadcasts table has no such limit; any module can
-- write any time. Phase 1 adds the *bookkeeping*; Phase 2 enforces.
--
-- Schema:
--   workspace_bandwidth_state — single row, current epoch + count
--   workspace_bandwidth_epochs — historical log of completed epochs
--
-- Phase 1 is inspection-only / additive. Phase 2 wires the limit
-- into workspace_ingest. Phase 3 lets the limit be context-modulated
-- (high arousal = wider bandwidth; consolidation = narrower).
--
-- Rollback:
--   DROP TABLE IF EXISTS workspace_bandwidth_epochs;
--   DROP TABLE IF EXISTS workspace_bandwidth_state;
--   DELETE FROM schema_version WHERE version = 72;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS workspace_bandwidth_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    epoch_started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    epoch_duration_seconds INTEGER NOT NULL DEFAULT 60 CHECK(epoch_duration_seconds > 0),
    epoch_count INTEGER NOT NULL DEFAULT 0,        -- broadcasts in the current epoch
    bandwidth_limit INTEGER NOT NULL DEFAULT 4 CHECK(bandwidth_limit > 0),
    total_admits INTEGER NOT NULL DEFAULT 0,
    total_rejects INTEGER NOT NULL DEFAULT 0,
    enforcement_mode TEXT NOT NULL DEFAULT 'shadow' CHECK(enforcement_mode IN ('shadow', 'enforce', 'disabled')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO workspace_bandwidth_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS workspace_bandwidth_epochs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch_started_at TEXT NOT NULL,
    epoch_ended_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    duration_seconds INTEGER NOT NULL,
    admitted_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    bandwidth_limit INTEGER NOT NULL,
    enforcement_mode TEXT NOT NULL,
    saturation REAL NOT NULL DEFAULT 0.0           -- admitted_count / bandwidth_limit
);
CREATE INDEX IF NOT EXISTS idx_wbe_recent ON workspace_bandwidth_epochs(epoch_ended_at);
CREATE INDEX IF NOT EXISTS idx_wbe_saturated ON workspace_bandwidth_epochs(saturation);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (72, 'workspace bandwidth Phase 1: 2 tables (state + epochs) for top-K-per-epoch limit',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
