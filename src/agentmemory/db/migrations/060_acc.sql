-- Migration 060: ACC — in-flight conflict / error monitor
-- Watches LIVE operations (memory_add, belief_set, entity_observe, workspace_broadcast)
-- and emits a scalar control-demand signal. Distinct from reflexion (after-fact lessons)
-- and from belief_conflicts (static contradictions in the DB).
-- Phase 1 is audit-only.
CREATE TABLE IF NOT EXISTS acc_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    agent_id TEXT,
    op_kind TEXT NOT NULL,
    op_scope TEXT,
    conflict_score REAL NOT NULL DEFAULT 0.0,
    surprise_score REAL NOT NULL DEFAULT 0.0,
    evc_score REAL NOT NULL DEFAULT 0.0,
    action TEXT NOT NULL DEFAULT 'log' CHECK(action IN ('log','warn','hold_fired','ignore')),
    fired_hold_id INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_acc_events_recent ON acc_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_acc_events_scope ON acc_events(op_scope, occurred_at DESC);

-- 5-second co-activation window: in-flight operations registered before commit.
CREATE TABLE IF NOT EXISTS acc_inflight (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    expires_at TEXT NOT NULL,
    agent_id TEXT,
    op_kind TEXT NOT NULL,
    op_scope TEXT NOT NULL,
    op_hash TEXT,
    intent_payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_acc_inflight_scope ON acc_inflight(op_scope, expires_at);

-- Learned outcome predictions per (op_kind, op_scope) — RVPM-style.
CREATE TABLE IF NOT EXISTS acc_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    op_kind TEXT NOT NULL,
    op_scope TEXT NOT NULL,
    n_trials INTEGER NOT NULL DEFAULT 0,
    n_conflicts INTEGER NOT NULL DEFAULT 0,
    p_conflict REAL NOT NULL DEFAULT 0.5,
    volatility REAL NOT NULL DEFAULT 0.5,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(op_kind, op_scope)
);
INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (60, 'ACC: in-flight conflict monitor (Botvinick + PRO)', strftime('%Y-%m-%dT%H:%M:%S','now'));
