-- Migration 057: cerebellum → workspace bridge
--
-- workspace_broadcasts.memory_id is NOT NULL with a FK to memories. The
-- cerebellum's boundary markers need to fire workspace broadcasts but
-- aren't attached to a specific memory. This migration seeds a sentinel
-- "cerebellum-system" memory that the cerebellum boundary broadcasts
-- reference. The sentinel is harmless: scope='system', category='environment',
-- low confidence; it never surfaces in regular search since the W(m) gate
-- would normally reject something this generic.
--
-- Rollback:
--   DELETE FROM workspace_broadcasts
--     WHERE triggered_by LIKE 'cerebellum_boundary:%';
--   DELETE FROM memories
--     WHERE scope='system' AND content LIKE 'cerebellum-system sentinel%';
--   DELETE FROM schema_version WHERE version = 57;
--
-- IDEMPOTENT.

-- Register the cerebellum-system agent (required for memory FK).
INSERT OR IGNORE INTO agents (
    id, display_name, agent_type, adapter_info, status
)
VALUES (
    'cerebellum-system',
    'Cerebellum (system)',
    'service',
    'auto-fires workspace broadcasts when cerebellum boundary markers cross threshold',
    'active'
);

-- Sentinel memory used as the FK target for cerebellum-fired workspace
-- broadcasts. Explicit ISO 8601 timestamps to satisfy memories table CHECK
-- constraints (the schema default uses datetime('now') with a space which
-- some installs reject).
INSERT OR IGNORE INTO memories (
    agent_id, category, content, scope, confidence, memory_type, write_tier,
    indexed, created_at, updated_at
)
VALUES (
    'cerebellum-system',
    'environment',
    'cerebellum-system sentinel: anchors workspace_broadcasts fired by '
    || 'cerebellum boundary markers. Do not surface in regular retrieval.',
    'system',
    0.1,
    'semantic',
    'construct',
    0,
    strftime('%Y-%m-%dT%H:%M:%S', 'now'),
    strftime('%Y-%m-%dT%H:%M:%S', 'now')
);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (57, 'cerebellum → workspace bridge sentinel memory',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
