-- Migration 055: basal ganglia Phase 2 — shadow-mode dispatch decision log
--
-- Phase 2 of the BG subsystem wires the TD-error broadcast bus into
-- outcome_annotate and adds a shadow consult at the tool-dispatch entry
-- point (mcp_server.py:3247). The shadow consult never alters dispatch
-- behavior; it records what the BG would have decided (approve / block /
-- delay / delegate) so we can validate the policy against actual outcomes
-- before flipping to enforcement mode.
--
-- Rollback, if needed:
--   DROP TABLE IF EXISTS bg_shadow_decisions;
--   DELETE FROM schema_version WHERE version = 55;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS bg_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    action_key TEXT NOT NULL,
    loop TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    net_signal REAL,
    w_go REAL,
    w_nogo REAL,
    context_hash TEXT,
    arguments_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_bg_shadow_recent
    ON bg_shadow_decisions(decision_at);

CREATE INDEX IF NOT EXISTS idx_bg_shadow_decision
    ON bg_shadow_decisions(decision, decision_at);

CREATE INDEX IF NOT EXISTS idx_bg_shadow_action
    ON bg_shadow_decisions(action_key, decision_at);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (55, 'basal ganglia Phase 2 shadow-mode dispatch decision log',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
