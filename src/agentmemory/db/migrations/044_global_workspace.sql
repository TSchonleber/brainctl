-- =============================================================================
-- Migration 044: Global Workspace Broadcasting
-- (RENUMBERED from original 013 in v2.2.0 — see migrate.py header comment
-- "DUPLICATE-VERSION HISTORY" for context. Inner schema_version INSERT
-- previously wrote 15, evidence of an earlier botched renumber attempt;
-- corrected to 44 to match the new file ordinal.)
-- Implements Baars' Global Workspace Theory as a salience-gated broadcast layer
-- on top of brain.db. High-salience memories "ignite" and broadcast org-wide.
-- Depends on: neuromodulation_state (012), memory_events/MEB (010)
-- IDEMPOTENT: every CREATE uses IF NOT EXISTS — safe to re-apply.
-- =============================================================================

PRAGMA foreign_keys = ON;

-- =============================================================================
-- WORKSPACE_CONFIG — ignition thresholds and governor settings
-- =============================================================================

CREATE TABLE IF NOT EXISTS workspace_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

-- Default configuration
INSERT OR IGNORE INTO workspace_config (key, value) VALUES
    ('ignition_threshold',        '0.85'),  -- normal ops: ignite above this
    ('urgent_threshold',          '0.65'),  -- URGENT/incident mode threshold
    ('governor_max_per_hour',     '20'),    -- broadcast storm guard
    ('broadcast_ttl_hours',       '48'),    -- how long broadcasts stay active
    ('phi_window_hours',          '24'),    -- integration metric window
    ('phi_warn_below',            '0.05'),  -- Phi drop alert threshold
    ('enabled',                   '1');     -- kill switch

-- =============================================================================
-- WORKSPACE_BROADCASTS — ignited memories broadcast to the org
-- =============================================================================

CREATE TABLE IF NOT EXISTS workspace_broadcasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       INTEGER NOT NULL REFERENCES memories(id),
    agent_id        TEXT    NOT NULL,                    -- who triggered the broadcast
    salience        REAL    NOT NULL,                    -- score that triggered ignition
    summary         TEXT    NOT NULL,                   -- short broadcast summary (≤200 chars)
    target_scope    TEXT    NOT NULL DEFAULT 'global',  -- 'global', 'project:X', 'agent:Y'
    broadcast_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    expires_at      TEXT,                               -- NULL = uses default TTL
    ack_count       INTEGER NOT NULL DEFAULT 0,
    triggered_by    TEXT    NOT NULL DEFAULT 'auto'     -- 'auto' | 'manual' | 'trigger'
);

CREATE INDEX IF NOT EXISTS idx_wb_broadcast_at   ON workspace_broadcasts(broadcast_at DESC);
CREATE INDEX IF NOT EXISTS idx_wb_memory_id      ON workspace_broadcasts(memory_id);
CREATE INDEX IF NOT EXISTS idx_wb_agent_id       ON workspace_broadcasts(agent_id);
CREATE INDEX IF NOT EXISTS idx_wb_target_scope   ON workspace_broadcasts(target_scope);
CREATE INDEX IF NOT EXISTS idx_wb_expires        ON workspace_broadcasts(expires_at);

-- =============================================================================
-- WORKSPACE_ACKS — agents acknowledge receipt of broadcasts
-- =============================================================================

CREATE TABLE IF NOT EXISTS workspace_acks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcast_id   INTEGER NOT NULL REFERENCES workspace_broadcasts(id),
    agent_id       TEXT    NOT NULL,
    acked_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(broadcast_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_wacks_broadcast ON workspace_acks(broadcast_id);
CREATE INDEX IF NOT EXISTS idx_wacks_agent     ON workspace_acks(agent_id);

-- Denormalize ack_count on broadcasts table
CREATE TRIGGER IF NOT EXISTS trg_ws_ack_count
AFTER INSERT ON workspace_acks
BEGIN
    UPDATE workspace_broadcasts
       SET ack_count = ack_count + 1
     WHERE id = NEW.broadcast_id;
END;

-- =============================================================================
-- WORKSPACE_PHI — organizational integration metric snapshots
-- =============================================================================

CREATE TABLE IF NOT EXISTS workspace_phi (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start     TEXT NOT NULL,
    window_end       TEXT NOT NULL,
    phi_org          REAL NOT NULL DEFAULT 0.0,   -- mean pair-wise integration
    broadcast_count  INTEGER NOT NULL DEFAULT 0,  -- broadcasts in window
    ack_rate         REAL NOT NULL DEFAULT 0.0,   -- fraction of broadcasts acked
    agent_pair_count INTEGER NOT NULL DEFAULT 0,  -- active agent pairs counted
    computed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_wphi_window ON workspace_phi(window_end DESC);

-- =============================================================================
-- AUTO-IGNITION TRIGGER — fires when a memory is inserted/updated
-- Computes salience inline; ignites (broadcasts) if above threshold.
-- Uses neuromodulation_state to pick normal vs urgent threshold.
-- =============================================================================

CREATE TRIGGER IF NOT EXISTS trg_memory_ignition_insert
AFTER INSERT ON memories
WHEN NEW.retired_at IS NULL
BEGIN
    -- Compute salience: priority signal (via category) + confidence + recency boost
    -- Categories map to implicit priority: decision/identity/convention = high
    -- We approximate salience from confidence since we don't have event priority here.
    -- Full salience scoring is done in Python; trigger handles high-confidence fast path.
    INSERT INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by)
    SELECT
        NEW.id,
        NEW.agent_id,
        NEW.confidence,
        substr(NEW.content, 1, 200),
        COALESCE(NEW.scope, 'global'),
        'auto'
    WHERE NEW.confidence >= COALESCE(
        -- Use urgent threshold if neuromod org_state = 'incident', else normal
        CASE
            WHEN EXISTS (
                SELECT 1 FROM neuromodulation_state WHERE id = 1 AND org_state = 'incident'
            ) THEN (SELECT CAST(value AS REAL) FROM workspace_config WHERE key = 'urgent_threshold')
            ELSE (SELECT CAST(value AS REAL) FROM workspace_config WHERE key = 'ignition_threshold')
        END,
        0.85
    )
    AND (SELECT value FROM workspace_config WHERE key = 'enabled') = '1'
    -- Governor: don't fire if we've already broadcast governor_max_per_hour in last hour
    AND (
        SELECT COUNT(*) FROM workspace_broadcasts
        WHERE broadcast_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-1 hour'))
    ) < CAST((SELECT value FROM workspace_config WHERE key = 'governor_max_per_hour') AS INTEGER);
END;

-- =============================================================================
-- SCHEMA VERSION
-- =============================================================================

INSERT INTO schema_version (version, description)
VALUES (44, 'Global Workspace Broadcasting — salience-gated org-wide awareness (renumbered from 013, was previously tagged 15)');
