-- Migration 053: thalamus Phase 2 shadow-mode decision log
--
-- Phase 2 of the thalamus subsystem (per docs/proposals/thalamus.md) adds
-- writeable gate / burst / mode tools and a shadow consult at the W(m) write
-- gate. The hookpoint never alters production behavior; it records what the
-- thalamic gate WOULD have done so we can compare against actual outcomes
-- before flipping to enforcement mode in a future phase.
--
-- This migration adds the append-only audit table that the shadow consult
-- writes to.
--
-- Rollback, if needed before live adoption:
--   DROP TABLE IF EXISTS thalamic_shadow_decisions;
--   DELETE FROM schema_version WHERE version = 53;
--
-- IDEMPOTENT: IF NOT EXISTS guards object creation.

CREATE TABLE IF NOT EXISTS thalamic_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    source_call TEXT NOT NULL,
    sector TEXT,
    channel_id TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    suppression REAL,
    bottomup_drive REAL,
    surprise_score REAL,
    actual_outcome TEXT,
    payload_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_shadow_recent
    ON thalamic_shadow_decisions(decision_at);

CREATE INDEX IF NOT EXISTS idx_shadow_sector_recent
    ON thalamic_shadow_decisions(sector, decision_at);

CREATE INDEX IF NOT EXISTS idx_shadow_decision_recent
    ON thalamic_shadow_decisions(decision, decision_at);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (53, 'thalamus Phase 2 shadow-mode decision log',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
