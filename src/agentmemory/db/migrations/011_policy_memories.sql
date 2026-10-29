-- Migration 011: Policy Memory Engine -- Implements the policy_memories table from architecture
-- Apply: sqlite3 ~/agentmemory/db/brain.db < ~/agentmemory/db/migrations/011_policy_memories.sql

CREATE TABLE IF NOT EXISTS policy_memories (
    policy_id               TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    category                TEXT NOT NULL DEFAULT 'general',
    status                  TEXT NOT NULL DEFAULT 'active'
                                CHECK(status IN ('candidate','active','deprecated')),
    scope                   TEXT NOT NULL DEFAULT 'global',
    priority                INTEGER NOT NULL DEFAULT 50,

    trigger_condition       TEXT NOT NULL,
    action_directive        TEXT NOT NULL,

    authored_by             TEXT NOT NULL DEFAULT 'unknown',
    derived_from            TEXT,

    confidence_threshold    REAL NOT NULL DEFAULT 0.5
                                CHECK(confidence_threshold >= 0.0 AND confidence_threshold <= 1.0),
    wisdom_half_life_days   INTEGER NOT NULL DEFAULT 30,
    version                 INTEGER NOT NULL DEFAULT 1,

    active_since            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    last_validated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    expires_at              TEXT,

    feedback_count          INTEGER NOT NULL DEFAULT 0,
    success_count           INTEGER NOT NULL DEFAULT 0,
    failure_count           INTEGER NOT NULL DEFAULT 0,

    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_pm_status_category ON policy_memories(status, category);
CREATE INDEX IF NOT EXISTS idx_pm_scope ON policy_memories(scope);
CREATE INDEX IF NOT EXISTS idx_pm_confidence ON policy_memories(confidence_threshold DESC);
CREATE INDEX IF NOT EXISTS idx_pm_priority ON policy_memories(priority DESC);
CREATE INDEX IF NOT EXISTS idx_pm_expires ON policy_memories(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pm_authored_by ON policy_memories(authored_by);

CREATE VIRTUAL TABLE IF NOT EXISTS policy_memories_fts USING fts5(
    trigger_condition,
    action_directive,
    name,
    content=policy_memories,
    content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS pm_fts_insert AFTER INSERT ON policy_memories BEGIN
    INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
    VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
END;

CREATE TRIGGER IF NOT EXISTS pm_fts_update AFTER UPDATE ON policy_memories BEGIN
    INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
    VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
    INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
    VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
END;

CREATE TRIGGER IF NOT EXISTS pm_fts_delete AFTER DELETE ON policy_memories BEGIN
    INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
    VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
END;

-- Seed Policy 1: Checkout Conflict — Do Not Retry
INSERT OR IGNORE INTO policy_memories (
    policy_id, name, category, scope, priority,
    trigger_condition, action_directive,
    authored_by, derived_from,
    confidence_threshold, wisdom_half_life_days,
    active_since, last_validated_at
) VALUES (
    'pol_seed_001_checkout_conflict',
    'coordination-checkout-conflict-guard',
    'coordination', 'global', 90,
    'Task checkout endpoint returns 409 Conflict when attempting to check out a task',
    'Do not retry the checkout. The task is owned by another agent. Move immediately to the next assigned task. Never attempt manual status overwrite, force-flag, or bypass to claim a conflicted task. Log the 409 as an observation event and continue.',
    'cortex',
    '["heartbeat-protocol","coordination-failure-class"]',
    0.97, 90,
    strftime('%Y-%m-%dT%H:%M:%S', 'now'),
    strftime('%Y-%m-%dT%H:%M:%S', 'now')
);

-- Seed Policy 2: Auth Identity Mismatch — Read-Only Until Corrected
INSERT OR IGNORE INTO policy_memories (
    policy_id, name, category, scope, priority,
    trigger_condition, action_directive,
    authored_by, derived_from,
    confidence_threshold, wisdom_half_life_days,
    active_since, last_validated_at
) VALUES (
    'pol_seed_002_auth_mismatch',
    'auth-identity-mismatch-guard',
    'coordination', 'global', 95,
    'AGENT_ID environment variable and the identity returned by GET /api/agents/me disagree — they resolve to different agent IDs',
    'Abort all mutating API calls (POST, PATCH, DELETE). Perform read-only checks only (GET requests). Do not checkout, update, or comment on any issue until the auth context is corrected. Log a warning event and alert the escalation chain.',
    'cortex',
    '["memory:85"]',
    1.0, 60,
    strftime('%Y-%m-%dT%H:%M:%S', 'now'),
    strftime('%Y-%m-%dT%H:%M:%S', 'now')
);

-- Seed Policy 3: Manager Role — Delegate, Do Not Execute
INSERT OR IGNORE INTO policy_memories (
    policy_id, name, category, scope, priority,
    trigger_condition, action_directive,
    authored_by, derived_from,
    confidence_threshold, wisdom_half_life_days,
    active_since, last_validated_at
) VALUES (
    'pol_seed_003_manager_delegation',
    'manager-delegates-not-executes',
    'routing', 'global', 75,
    'A manager-level agent (Hermes, Engram, Legion, or any agent with direct reports) receives a task that involves ground-level execution work (coding, file editing, data processing, API calls)',
    'Do not execute the ground-level work directly. Decompose the task, create subtasks and assign to the appropriate IC agents. The manager role is to define success criteria, unblock ICs, and report up — not to grind through implementation. Exception: quick one-off reads or searches that would take longer to delegate than do.',
    'cortex',
    '["event:50","event:44"]',
    0.85, 90,
    strftime('%Y-%m-%dT%H:%M:%S', 'now'),
    strftime('%Y-%m-%dT%H:%M:%S', 'now')
);
