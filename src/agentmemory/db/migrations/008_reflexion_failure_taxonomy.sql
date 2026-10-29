-- Migration 008: Reflexion Failure Taxonomy
-- Implements — classify, store, and cross-propagate failure lessons.
-- Adds reflexion_lessons table with FTS5 index, expiration lifecycle,
-- cross-agent generalization, and confidence evolution mechanics.

-- Primary lessons table
CREATE TABLE IF NOT EXISTS reflexion_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity / provenance
    source_agent_id TEXT NOT NULL REFERENCES agents(id),
    source_event_id INTEGER REFERENCES events(id),
    source_run_id TEXT,

    -- Failure classification
    failure_class TEXT NOT NULL
        CHECK (failure_class IN (
            'REASONING_ERROR',
            'CONTEXT_LOSS',
            'HALLUCINATION',
            'COORDINATION_FAILURE',
            'TOOL_MISUSE'
        )),
    failure_subclass TEXT,

    -- Trigger conditions
    trigger_conditions TEXT NOT NULL,

    -- Lesson content
    lesson_content TEXT NOT NULL,

    -- Generalization scope (JSON array: "agent_type:external", "capability:brainctl", etc.)
    generalizable_to TEXT NOT NULL DEFAULT '[]',

    -- Lifecycle
    confidence REAL NOT NULL DEFAULT 0.8
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    override_level TEXT NOT NULL DEFAULT 'SOFT_HINT'
        CHECK (override_level IN ('HARD_OVERRIDE', 'SOFT_HINT', 'SILENT_LOG')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived', 'retired')),

    -- Expiration policy
    expiration_policy TEXT NOT NULL DEFAULT 'success_count'
        CHECK (expiration_policy IN ('success_count', 'code_fix', 'ttl', 'manual')),
    expiration_n INTEGER DEFAULT 5,
    expiration_ttl_days INTEGER,
    root_cause_ref TEXT,
    consecutive_successes INTEGER NOT NULL DEFAULT 0,
    last_validated_at TEXT,

    -- Retrieval stats
    times_retrieved INTEGER NOT NULL DEFAULT 0,
    times_prevented_failure INTEGER NOT NULL DEFAULT 0,
    times_failed_to_prevent INTEGER NOT NULL DEFAULT 0,

    -- Timestamps
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at TEXT,
    retired_at TEXT,
    retirement_reason TEXT
);

-- Indexes for retrieval
CREATE INDEX IF NOT EXISTS idx_rlessons_agent
    ON reflexion_lessons(source_agent_id);
CREATE INDEX IF NOT EXISTS idx_rlessons_failure_class
    ON reflexion_lessons(failure_class);
CREATE INDEX IF NOT EXISTS idx_rlessons_status
    ON reflexion_lessons(status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_rlessons_confidence
    ON reflexion_lessons(confidence DESC);
CREATE INDEX IF NOT EXISTS idx_rlessons_generalizable
    ON reflexion_lessons(generalizable_to);
CREATE INDEX IF NOT EXISTS idx_rlessons_active_class
    ON reflexion_lessons(status, failure_class, confidence DESC)
    WHERE status = 'active';

-- FTS5 full-text index over trigger conditions and lesson content
CREATE VIRTUAL TABLE IF NOT EXISTS reflexion_lessons_fts USING fts5(
    trigger_conditions,
    lesson_content,
    failure_class,
    failure_subclass,
    content=reflexion_lessons,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- FTS5 sync triggers
CREATE TRIGGER IF NOT EXISTS rlessons_fts_insert AFTER INSERT ON reflexion_lessons BEGIN
    INSERT INTO reflexion_lessons_fts(rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES (new.id, new.trigger_conditions, new.lesson_content, new.failure_class, new.failure_subclass);
END;

CREATE TRIGGER IF NOT EXISTS rlessons_fts_update AFTER UPDATE ON reflexion_lessons BEGIN
    INSERT INTO reflexion_lessons_fts(reflexion_lessons_fts, rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES ('delete', old.id, old.trigger_conditions, old.lesson_content, old.failure_class, old.failure_subclass);
    INSERT INTO reflexion_lessons_fts(rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES (new.id, new.trigger_conditions, new.lesson_content, new.failure_class, new.failure_subclass);
END;

CREATE TRIGGER IF NOT EXISTS rlessons_fts_delete AFTER DELETE ON reflexion_lessons BEGIN
    INSERT INTO reflexion_lessons_fts(reflexion_lessons_fts, rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES ('delete', old.id, old.trigger_conditions, old.lesson_content, old.failure_class, old.failure_subclass);
END;

-- Updated_at trigger
CREATE TRIGGER IF NOT EXISTS rlessons_updated_at AFTER UPDATE ON reflexion_lessons BEGIN
    UPDATE reflexion_lessons SET updated_at = datetime('now') WHERE id = new.id;
END;

-- Schema version bump
INSERT INTO schema_version (version, description)
VALUES (8, 'Reflexion failure taxonomy — reflexion_lessons table with FTS5, lifecycle, and cross-agent generalization ');
