-- Migration 058: amygdala subsystem — Phase 1 schema
--
-- Fourth brain-inspired subsystem after thalamus, basal ganglia, cerebellum
-- (all shipped 2026-05-15). The amygdala adds rapid one-shot valence/threat
-- tagging that turns ephemeral affect classifications into durable per-
-- entity / per-agent / per-context valence scores. Per McGaugh: this layer
-- does NOT store memories — it MODULATES consolidation, retrieval, and
-- broadcast salience elsewhere.
--
-- Three tables encode the BLA + CeA + ITC split from biology:
--   amygdala_valence_tags    — BLA-analog associative store (per target)
--   amygdala_valence_events  — audit trail of all updates
--   amygdala_extinction_gates — ITC-analog context-keyed inhibitory overlays
--
-- Phase 1 is schema + 4 tools (manual usage). Phase 2 wires auto-tagging on
-- memory_add and connects to hippocampus replay_priority via the existing
-- consolidation_priority() function (currently dead code in affect.py).
--
-- Rollback:
--   DROP TABLE IF EXISTS amygdala_extinction_gates;
--   DROP TABLE IF EXISTS amygdala_valence_events;
--   DROP TABLE IF EXISTS amygdala_valence_tags;
--   DELETE FROM schema_version WHERE version = 58;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS amygdala_valence_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('entity', 'agent', 'context')),
    target_id TEXT NOT NULL,
    valence REAL NOT NULL DEFAULT 0.0,
    arousal REAL NOT NULL DEFAULT 0.0,
    n_updates INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    labile_until TEXT,
    UNIQUE (target_kind, target_id)
);
CREATE INDEX IF NOT EXISTS idx_amyg_tags_kind ON amygdala_valence_tags(target_kind);
CREATE INDEX IF NOT EXISTS idx_amyg_tags_labile ON amygdala_valence_tags(labile_until)
    WHERE labile_until IS NOT NULL;

CREATE TABLE IF NOT EXISTS amygdala_valence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    valence_delta REAL NOT NULL,
    arousal REAL NOT NULL,
    source_memory_id INTEGER,
    source_event_id INTEGER,
    reason TEXT,
    learning_rate REAL NOT NULL DEFAULT 0.1,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_amyg_events_target ON amygdala_valence_events(target_kind, target_id, fired_at);
CREATE INDEX IF NOT EXISTS idx_amyg_events_recent ON amygdala_valence_events(fired_at);

CREATE TABLE IF NOT EXISTS amygdala_extinction_gates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    suppression_level REAL NOT NULL DEFAULT 0.5 CHECK(suppression_level >= 0.0 AND suppression_level <= 1.0),
    n_safe_exposures INTEGER NOT NULL DEFAULT 1,
    installed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE (target_kind, target_id, context_hash)
);
CREATE INDEX IF NOT EXISTS idx_amyg_gates_target ON amygdala_extinction_gates(target_kind, target_id);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (58, 'amygdala Phase 1: valence tags, events, extinction gates',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
