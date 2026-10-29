-- Migration 010: Memory Event Bus (MEB)
-- Author: Weaver (Context Integration Engineer)
-- Date: 2026-03-28
-- Purpose: Implement the Memory Event Bus — a lightweight SQLite-native propagation
--          layer that captures all memory writes as subscribable events, enabling
--          sub-500ms cross-agent notification without external message brokers.
-- References: (this implementation), (propagation spec)
-- (Memory as Policy Engine — policy updates need fast propagation)
-- Schema version: 9 -> 10

-- ---------------------------------------------------------------------------
-- 1. memory_events table
--    Captures every memory INSERT and UPDATE as a discrete propagation event.
--    Agents poll this table (brainctl meb tail --since <last_id>) to learn
--    about new or updated memories without reloading the full memory spine.
-- ---------------------------------------------------------------------------
CREATE TABLE memory_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id      INTEGER NOT NULL REFERENCES memories(id),
    agent_id       TEXT    NOT NULL,          -- agent that wrote the memory
    operation      TEXT    NOT NULL DEFAULT 'insert',  -- 'insert' | 'update'
    category       TEXT    NOT NULL,          -- mirrors memories.category at write time
    scope          TEXT    NOT NULL,          -- mirrors memories.scope at write time
    memory_type    TEXT    NOT NULL DEFAULT 'episodic',  -- 'episodic' | 'semantic'
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    ttl_expires_at TEXT                       -- set by prune; NULL = no expiry override
);

-- Fast lookups for agent polling (most common access pattern)
CREATE INDEX idx_meb_id_asc     ON memory_events(id ASC);
CREATE INDEX idx_meb_agent      ON memory_events(agent_id);
CREATE INDEX idx_meb_category   ON memory_events(category);
CREATE INDEX idx_meb_scope      ON memory_events(scope);
CREATE INDEX idx_meb_created_at ON memory_events(created_at DESC);
CREATE INDEX idx_meb_ttl        ON memory_events(ttl_expires_at)
    WHERE ttl_expires_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Triggers: auto-populate memory_events on every memory write
-- ---------------------------------------------------------------------------

-- INSERT trigger: fires whenever a new memory is added to the spine
CREATE TRIGGER meb_after_memory_insert
AFTER INSERT ON memories
BEGIN
    INSERT INTO memory_events (memory_id, agent_id, operation, category, scope, memory_type, created_at)
    VALUES (
        new.id,
        new.agent_id,
        'insert',
        new.category,
        new.scope,
        COALESCE(new.memory_type, 'episodic'),
        strftime('%Y-%m-%dT%H:%M:%S', 'now')
    );
END;

-- UPDATE trigger: fires when content, category, scope, confidence, or trust_score changes.
-- Only tracks meaningful field updates — not administrative fields (recalled_count, updated_at).
CREATE TRIGGER meb_after_memory_update
AFTER UPDATE OF content, category, scope, confidence, trust_score, memory_type ON memories
WHEN new.retired_at IS NULL
BEGIN
    INSERT INTO memory_events (memory_id, agent_id, operation, category, scope, memory_type, created_at)
    VALUES (
        new.id,
        new.agent_id,
        'update',
        new.category,
        new.scope,
        COALESCE(new.memory_type, 'episodic'),
        strftime('%Y-%m-%dT%H:%M:%S', 'now')
    );
END;

-- ---------------------------------------------------------------------------
-- 3. meb_config table
--    Stores MEB-wide tuning parameters (TTL, max queue depth) so they can be
--    adjusted without redeploying brainctl.
-- ---------------------------------------------------------------------------
CREATE TABLE meb_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

INSERT INTO meb_config (key, value) VALUES
    ('ttl_hours',       '72'),       -- events older than 72h are prunable
    ('max_queue_depth', '10000'),    -- hard cap on memory_events rows
    ('prune_on_read',   'true');     -- auto-prune TTL'd rows on meb tail/stats

-- ---------------------------------------------------------------------------
-- 4. Backfill: seed memory_events from existing memories so subscribers
--    immediately have a baseline. Mark as operation='backfill'.
-- ---------------------------------------------------------------------------
INSERT INTO memory_events (memory_id, agent_id, operation, category, scope, memory_type, created_at)
SELECT
    id,
    agent_id,
    'backfill',
    category,
    scope,
    COALESCE(memory_type, 'episodic'),
    created_at
FROM memories
WHERE retired_at IS NULL
ORDER BY id ASC;

-- ---------------------------------------------------------------------------
-- 5. Schema version bump
-- ---------------------------------------------------------------------------
INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (10, strftime('%Y-%m-%dT%H:%M:%S', 'now'),
  'Memory Event Bus (MEB): memory_events table + INSERT/UPDATE triggers + meb_config — ');

PRAGMA user_version = 10;
