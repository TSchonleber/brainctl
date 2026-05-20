-- Migration 082: olfactory cortex — Phase 1 schema
--
-- Olfactory cortex is the ONE sensory modality that bypasses thalamus.
-- Olfactory bulb projects directly to piriform cortex + amygdala +
-- entorhinal cortex. This direct route is why smells produce such
-- strong emotional/memory recall (Proust effect).
--
-- brainctl analog: a "direct binding" channel that, by-passing the
-- normal thalamus → cortex → amygdala flow, immediately binds an
-- incoming content type to a stored valence + an episodic memory
-- pointer. Useful for input modalities where the brain decides this
-- pattern is too primal for the standard W(m) gate.
--
-- Phase 1 ships:
--   olfactory_imprints — direct (content_hash, valence, memory_id)
--                        bindings that bypass standard write gates
--   olfactory_state — single row tracking total imprints + rate
--
-- Phase 2 wires olfactory_imprint into amygdala_tag for the bypass
-- path. Phase 3 lets olfactory_query return bound memories directly
-- (Proust-style fast emotional recall).
--
-- Rollback:
--   DROP TABLE IF EXISTS olfactory_imprints;
--   DROP TABLE IF EXISTS olfactory_state;
--   DELETE FROM schema_version WHERE version = 82;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS olfactory_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_imprints INTEGER NOT NULL DEFAULT 0,
    enforcement_mode TEXT NOT NULL DEFAULT 'shadow' CHECK(enforcement_mode IN (
        'shadow', 'enforce', 'disabled'
    )),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO olfactory_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS olfactory_imprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imprinted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    content_hash TEXT NOT NULL,
    content_kind TEXT,           -- e.g. 'text_pattern', 'entity_name', 'phrase'
    valence REAL NOT NULL CHECK(valence BETWEEN -1.0 AND 1.0),
    arousal REAL NOT NULL DEFAULT 0.5 CHECK(arousal BETWEEN 0.0 AND 1.0),
    bound_memory_id INTEGER,     -- optional memory pointer this imprint resurrects
    bound_entity_id INTEGER,     -- optional entity pointer
    agent_id TEXT,
    times_recalled INTEGER NOT NULL DEFAULT 0,
    last_recalled_at TEXT,
    notes TEXT,
    UNIQUE (content_hash, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_oi_recent ON olfactory_imprints(imprinted_at);
CREATE INDEX IF NOT EXISTS idx_oi_content ON olfactory_imprints(content_hash);
CREATE INDEX IF NOT EXISTS idx_oi_valence ON olfactory_imprints(valence);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (82, 'olfactory cortex Phase 1: direct sensory-emotional binding (2 tables)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
