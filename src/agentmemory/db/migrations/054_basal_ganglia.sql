-- Migration 054: basal ganglia subsystem — Phase 1 schema
--
-- Implements Phase 1 of the BG proposal at docs/proposals/basal_ganglia.md.
-- The BG sits upstream of the thalamus in the call path:
--   agent request → BG (action selection, outcome-driven RL) → thalamus
--   (typed routing, gating) → substrate
--
-- Phase 1 is inspection-only / additive: schema + read-and-CRUD tools.
-- No existing tool behavior changes. The TD-error broadcast bus and
-- eligibility-trace updates exist as tables but aren't wired into
-- mcp_server.py:tool_call_handler yet — that's Phase 2 (shadow gate).
--
-- Five biological invariants encoded here (see proposal):
--   1. Five parallel topographic loops (motor/oculomotor/dlpfc/lofc/acc)
--   2. Opponent Go/NoGo weights per (action, context)
--   3. Distributional value as 5 expectile estimates per row
--   4. Eligibility traces with decay constants
--   5. Single-row global modulator state (tonic DA / LC-NE / 5-HT)
--
-- Rollback, if needed before live adoption:
--   DROP TABLE IF EXISTS bg_chunks;
--   DROP TABLE IF EXISTS bg_holds;
--   DROP TABLE IF EXISTS bg_modulators;
--   DROP TABLE IF EXISTS bg_td_events;
--   DROP TABLE IF EXISTS bg_eligibility_traces;
--   DROP TABLE IF EXISTS bg_striatal_weights;
--   DROP TABLE IF EXISTS bg_actions;
--   DELETE FROM schema_version WHERE version = 54;
--
-- IDEMPOTENT: IF NOT EXISTS guards object creation; seed rows use
-- INSERT OR IGNORE so repeated application does not duplicate state.

-- Candidate action catalog: one row per "thing the BG can gate"
CREATE TABLE IF NOT EXISTS bg_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT NOT NULL CHECK(loop IN ('motor','oculomotor','dlpfc','lofc','acc')),
    action_key TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE (loop, action_key)
);
CREATE INDEX IF NOT EXISTS idx_bg_actions_loop ON bg_actions(loop);

-- Striatal weights: opponent Go / NoGo + 5-expectile distributional value
-- keyed by (action, context). context_hash is a stable hash of relevant
-- state features (project, agent, recent outcomes, neurostate mode).
CREATE TABLE IF NOT EXISTS bg_striatal_weights (
    action_id INTEGER NOT NULL,
    context_hash TEXT NOT NULL,
    w_go REAL NOT NULL DEFAULT 0.0,
    w_nogo REAL NOT NULL DEFAULT 0.0,
    v_q10 REAL NOT NULL DEFAULT 0.0,
    v_q30 REAL NOT NULL DEFAULT 0.0,
    v_q50 REAL NOT NULL DEFAULT 0.0,
    v_q70 REAL NOT NULL DEFAULT 0.0,
    v_q90 REAL NOT NULL DEFAULT 0.0,
    n_updates INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (action_id, context_hash),
    FOREIGN KEY (action_id) REFERENCES bg_actions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_bg_weights_action ON bg_striatal_weights(action_id);
CREATE INDEX IF NOT EXISTS idx_bg_weights_ctx ON bg_striatal_weights(context_hash);

-- Eligibility traces: transient tags deposited by gating decisions, decayed
-- and swept periodically by bg_sweep_traces.
CREATE TABLE IF NOT EXISTS bg_eligibility_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL,
    context_hash TEXT NOT NULL,
    trace_strength REAL NOT NULL DEFAULT 1.0,
    decay_constant REAL NOT NULL DEFAULT 0.95,
    decision_event_id INTEGER,
    deposited_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    expires_at TEXT,
    FOREIGN KEY (action_id) REFERENCES bg_actions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_bg_traces_active ON bg_eligibility_traces(expires_at);
CREATE INDEX IF NOT EXISTS idx_bg_traces_ctx ON bg_eligibility_traces(action_id, context_hash);

-- TD-error event log: the dopamine broadcast bus.
-- δ = utility(outcome) + γ·V(s') − V(s)
CREATE TABLE IF NOT EXISTS bg_td_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    agent_id TEXT,
    utility REAL NOT NULL,
    v_current REAL NOT NULL DEFAULT 0.0,
    v_next REAL NOT NULL DEFAULT 0.0,
    gamma REAL NOT NULL DEFAULT 0.95,
    delta REAL NOT NULL,
    source TEXT NOT NULL,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    consumed_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bg_td_recent ON bg_td_events(fired_at);
CREATE INDEX IF NOT EXISTS idx_bg_td_agent ON bg_td_events(agent_id, fired_at);

-- Hyperdirect "hold" events: global pauses triggered by conflict, surprise,
-- or explicit stop signals.
CREATE TABLE IF NOT EXISTS bg_holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('conflict','surprise','explicit_stop')),
    trigger_score_gap REAL,
    ticks INTEGER NOT NULL DEFAULT 1,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bg_holds_active ON bg_holds(released_at);
CREATE INDEX IF NOT EXISTS idx_bg_holds_loop ON bg_holds(loop, fired_at);

-- Neuromodulator dials (single row, broadcast). Three independent knobs,
-- NOT one temperature scalar (per BG research swarm finding):
--   tonic_da: policy vigor / search breadth (exploit vs explore)
--   lc_ne:    arousal / surprise gain (broaden eligibility under high)
--   serotonin: time horizon, γ scaling (myopic vs patient)
CREATE TABLE IF NOT EXISTS bg_modulators (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tonic_da REAL NOT NULL DEFAULT 0.5,
    lc_ne REAL NOT NULL DEFAULT 0.5,
    serotonin REAL NOT NULL DEFAULT 0.5,
    set_by TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO bg_modulators (id) VALUES (1);

-- Action-chunk catalog (Graybiel task-bracketing): durable start/stop
-- markers around opaque action sequences. Atomic from the selector's
-- perspective once formed.
CREATE TABLE IF NOT EXISTS bg_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop TEXT NOT NULL,
    name TEXT NOT NULL,
    start_marker TEXT NOT NULL,
    end_marker TEXT NOT NULL,
    body_actions_json TEXT,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE (loop, name)
);
CREATE INDEX IF NOT EXISTS idx_bg_chunks_loop ON bg_chunks(loop);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (54, 'basal ganglia Phase 1: 7 tables (actions, weights, traces, td_events, holds, modulators, chunks)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
