-- Migration 056: cerebellum subsystem — Phase 1 schema
--
-- Third brain-inspired subsystem after thalamus and basal ganglia. Implements
-- a forward-model layer that issues predictions before actions commit and
-- learns from observed errors (Marr-Albus + Kawato MPFIM/MOSAIC).
--
-- Five cortical partner modules mirror BG's five loops:
--   motor_partner       — predicts outcomes of state-mutating actions
--   oculomotor_partner  — predicts retrieval relevance
--   dlpfc_partner       — predicts plan-step completion / result shape
--   lofc_partner        — predicts expected utility / outcome class
--   acc_partner         — predicts conflict probability
--
-- Each (partner, prediction_kind) pair is a module; weights are a sparse
-- linear readout over hashed context features (granule-cell expansion).
--
-- Phase 1 is inspection + manual setup only. Phase 2 wires the predict /
-- observe loop into the dispatch shadow consult; Phase 3 modulates thalamic
-- precision; Phase 4 enforces.
--
-- Rollback, if needed:
--   DROP TABLE IF EXISTS cerebellum_boundaries;
--   DROP TABLE IF EXISTS cerebellum_traces;
--   DROP TABLE IF EXISTS cerebellum_predictions;
--   DROP TABLE IF EXISTS cerebellum_weights;
--   DROP TABLE IF EXISTS cerebellum_modules;
--   DELETE FROM schema_version WHERE version = 56;
--
-- IDEMPOTENT: IF NOT EXISTS + INSERT OR IGNORE.

CREATE TABLE IF NOT EXISTS cerebellum_modules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner TEXT NOT NULL CHECK(partner IN (
        'motor_partner', 'oculomotor_partner', 'dlpfc_partner',
        'lofc_partner', 'acc_partner'
    )),
    prediction_kind TEXT NOT NULL CHECK(prediction_kind IN (
        'success_probability', 'expected_latency_ms', 'expected_outcome_class'
    )),
    description TEXT,
    n_predictions INTEGER NOT NULL DEFAULT 0,
    mean_abs_error REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE (partner, prediction_kind)
);
CREATE INDEX IF NOT EXISTS idx_cb_modules_partner ON cerebellum_modules(partner);

CREATE TABLE IF NOT EXISTS cerebellum_weights (
    module_id INTEGER NOT NULL,
    context_hash TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.0,
    n_updates INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (module_id, context_hash),
    FOREIGN KEY (module_id) REFERENCES cerebellum_modules(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cb_weights_module ON cerebellum_weights(module_id);

CREATE TABLE IF NOT EXISTS cerebellum_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id INTEGER NOT NULL,
    context_hash TEXT NOT NULL,
    predicted_value REAL NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    decision_event_id INTEGER,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    observed_value REAL,
    observed_at TEXT,
    delta_forward REAL,
    FOREIGN KEY (module_id) REFERENCES cerebellum_modules(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cb_pred_recent ON cerebellum_predictions(fired_at);
CREATE INDEX IF NOT EXISTS idx_cb_pred_module ON cerebellum_predictions(module_id, fired_at);
CREATE INDEX IF NOT EXISTS idx_cb_pred_pending
    ON cerebellum_predictions(observed_at) WHERE observed_at IS NULL;

CREATE TABLE IF NOT EXISTS cerebellum_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id INTEGER NOT NULL,
    context_hash TEXT NOT NULL,
    prediction_id INTEGER,
    trace_strength REAL NOT NULL DEFAULT 1.0,
    decay_constant REAL NOT NULL DEFAULT 0.95,
    deposited_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    expires_at TEXT,
    FOREIGN KEY (module_id) REFERENCES cerebellum_modules(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cb_traces_active ON cerebellum_traces(expires_at);

CREATE TABLE IF NOT EXISTS cerebellum_boundaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner TEXT NOT NULL,
    delta_forward REAL NOT NULL,
    context_hash TEXT NOT NULL,
    prediction_id INTEGER,
    salience REAL NOT NULL,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    consumed_by TEXT,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cb_boundaries_recent ON cerebellum_boundaries(fired_at);
CREATE INDEX IF NOT EXISTS idx_cb_boundaries_unconsumed
    ON cerebellum_boundaries(consumed_at) WHERE consumed_at IS NULL;

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (56, 'cerebellum Phase 1: 5 tables (modules, weights, predictions, traces, boundaries)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
