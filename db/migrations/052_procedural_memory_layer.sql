PRAGMA foreign_keys = OFF;
BEGIN;

DROP TRIGGER IF EXISTS memories_fts_insert;
DROP TRIGGER IF EXISTS memories_fts_update_delete;
DROP TRIGGER IF EXISTS memories_fts_update_insert;
DROP TRIGGER IF EXISTS memories_fts_delete;
DROP TRIGGER IF EXISTS memories_temporal_class_check;
DROP TRIGGER IF EXISTS memories_temporal_class_update_check;
DROP TRIGGER IF EXISTS memories_validate_ts_insert;
DROP TRIGGER IF EXISTS memories_validate_ts_update;
DROP TRIGGER IF EXISTS meb_after_memory_insert;
DROP TRIGGER IF EXISTS meb_after_memory_update;
DROP TRIGGER IF EXISTS trg_memory_ignition_insert;
DROP TRIGGER IF EXISTS trg_gw_broadcast_meb;
DROP TRIGGER IF EXISTS trg_gw_broadcast_workspace;
DROP TRIGGER IF EXISTS memories_visibility_check_insert;
DROP TRIGGER IF EXISTS memories_visibility_check_update;
DROP TRIGGER IF EXISTS trg_memory_delete_cascade_edges;
DROP TRIGGER IF EXISTS trg_agent_delete_nullify_validation;
DROP VIEW IF EXISTS decoherent_memories;
DROP TABLE IF EXISTS memories_fts;

CREATE TEMP TABLE memories_backup AS
SELECT
    id, agent_id, category, scope, content, confidence, source_event_id,
    supersedes_id, tags, expires_at, recalled_count, last_recalled_at,
    created_at, updated_at, retired_at, epoch_id, temporal_class,
    validation_agent_id, validated_at, trust_score, derived_from_ids,
    retracted_at, retraction_reason, version, memory_type, protected,
    salience_score, gw_broadcast, visibility, read_acl, ewc_importance,
    alpha, beta, confidence_phase, hilbert_projection, coherence_syndrome,
    decoherence_rate, gated_from_memory_id, file_path, file_line, write_tier,
    indexed, promoted_at, replay_priority, ripple_tags, labile_until,
    labile_agent_id, retrieval_prediction_error, encoding_affect_id,
    tag_cycles_remaining, stability, encoding_task_context,
    encoding_context_hash, temporal_level, next_review_at, q_value
FROM memories;

DROP TABLE memories;

CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),   -- who wrote this
    category TEXT NOT NULL,                          -- 'identity', 'user', 'environment', 'convention',
                                                     -- 'project', 'decision', 'lesson', 'preference'
    scope TEXT NOT NULL DEFAULT 'global',            -- 'global', 'project:<name>', 'agent:<id>'
    content TEXT NOT NULL,                           -- the actual memory
    confidence REAL NOT NULL DEFAULT 1.0,            -- 0.0-1.0, decays or gets boosted
    source_event_id INTEGER,                         -- event that spawned this memory
    supersedes_id INTEGER REFERENCES memories(id),   -- if this replaces an older memory
    tags TEXT,                                        -- JSON array of tags
    expires_at TEXT,                                  -- optional TTL
    recalled_count INTEGER NOT NULL DEFAULT 0,        -- how often this memory was retrieved
    last_recalled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    retired_at TEXT,                                   -- soft delete
    epoch_id INTEGER REFERENCES epochs(id),
    temporal_class TEXT NOT NULL DEFAULT 'medium',
    validation_agent_id TEXT REFERENCES agents(id),
    validated_at TEXT,
    trust_score REAL DEFAULT 1.0,
    derived_from_ids TEXT,
    retracted_at TEXT,
    retraction_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    memory_type TEXT NOT NULL DEFAULT 'episodic' CHECK(memory_type IN ('episodic','semantic','procedural')),
    protected INTEGER NOT NULL DEFAULT 0,
    salience_score REAL NOT NULL DEFAULT 0.0,
    gw_broadcast INTEGER NOT NULL DEFAULT 0,
    visibility TEXT NOT NULL DEFAULT 'public',
    read_acl TEXT,
    ewc_importance REAL NOT NULL DEFAULT 0.0,
    alpha REAL DEFAULT 1.0,
    beta  REAL DEFAULT 1.0,
    confidence_alpha REAL GENERATED ALWAYS AS (alpha) VIRTUAL,
    confidence_beta  REAL GENERATED ALWAYS AS (beta)  VIRTUAL,
    confidence_phase REAL NOT NULL DEFAULT 0.0,
    hilbert_projection BLOB DEFAULT NULL,
    coherence_syndrome TEXT DEFAULT NULL,
    decoherence_rate REAL DEFAULT NULL,
    gated_from_memory_id INTEGER REFERENCES memories(id),
    file_path TEXT,
    file_line INTEGER,
    write_tier TEXT NOT NULL DEFAULT 'full' CHECK(write_tier IN ('skip', 'construct', 'full')),
    indexed INTEGER NOT NULL DEFAULT 1,
    promoted_at TEXT DEFAULT NULL,
    replay_priority REAL NOT NULL DEFAULT 0.0,
    ripple_tags INTEGER NOT NULL DEFAULT 0,
    labile_until TEXT DEFAULT NULL,
    labile_agent_id TEXT DEFAULT NULL,
    retrieval_prediction_error REAL DEFAULT NULL,
    encoding_affect_id INTEGER REFERENCES affect_log(id) DEFAULT NULL,
    tag_cycles_remaining INTEGER DEFAULT 0,
    stability REAL DEFAULT 1.0,
    encoding_task_context TEXT DEFAULT NULL,
    encoding_context_hash TEXT DEFAULT NULL,
    temporal_level TEXT NOT NULL DEFAULT 'moment'
        CHECK(temporal_level IN ('moment','session','day','week','month','quarter')),
    next_review_at TEXT DEFAULT NULL,
    q_value REAL DEFAULT 0.5
);

INSERT INTO memories (
    id, agent_id, category, scope, content, confidence, source_event_id,
    supersedes_id, tags, expires_at, recalled_count, last_recalled_at,
    created_at, updated_at, retired_at, epoch_id, temporal_class,
    validation_agent_id, validated_at, trust_score, derived_from_ids,
    retracted_at, retraction_reason, version, memory_type, protected,
    salience_score, gw_broadcast, visibility, read_acl, ewc_importance,
    alpha, beta, confidence_phase, hilbert_projection, coherence_syndrome,
    decoherence_rate, gated_from_memory_id, file_path, file_line, write_tier,
    indexed, promoted_at, replay_priority, ripple_tags, labile_until,
    labile_agent_id, retrieval_prediction_error, encoding_affect_id,
    tag_cycles_remaining, stability, encoding_task_context,
    encoding_context_hash, temporal_level, next_review_at, q_value
)
SELECT
    id, agent_id, category, scope, content, confidence, source_event_id,
    supersedes_id, tags, expires_at, recalled_count, last_recalled_at,
    created_at, updated_at, retired_at, epoch_id, temporal_class,
    validation_agent_id, validated_at, trust_score, derived_from_ids,
    retracted_at, retraction_reason, version, memory_type, protected,
    salience_score, gw_broadcast, visibility, read_acl, ewc_importance,
    alpha, beta, confidence_phase, hilbert_projection, coherence_syndrome,
    decoherence_rate, gated_from_memory_id, file_path, file_line, write_tier,
    indexed, promoted_at, replay_priority, ripple_tags, labile_until,
    labile_agent_id, retrieval_prediction_error, encoding_affect_id,
    tag_cycles_remaining, stability, encoding_task_context,
    encoding_context_hash, temporal_level, next_review_at, q_value
FROM memories_backup;

DROP TABLE memories_backup;

CREATE INDEX idx_memories_agent ON memories(agent_id);
CREATE INDEX idx_memories_category ON memories(category);
CREATE INDEX idx_memories_scope ON memories(scope);
CREATE INDEX idx_memories_active ON memories(retired_at) WHERE retired_at IS NULL;
CREATE INDEX idx_memories_confidence ON memories(confidence DESC);
CREATE INDEX idx_memories_agent_active_cat ON memories(agent_id, category) WHERE retired_at IS NULL;
CREATE INDEX idx_memories_agent_time ON memories(agent_id, created_at DESC) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_encoding_affect
    ON memories(encoding_affect_id) WHERE encoding_affect_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_context_hash
    ON memories(encoding_context_hash) WHERE encoding_context_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_next_review
    ON memories(next_review_at) WHERE next_review_at IS NOT NULL AND retired_at IS NULL;
CREATE INDEX idx_memories_epoch ON memories(epoch_id);
CREATE INDEX idx_memories_temporal_class ON memories(temporal_class);
CREATE INDEX idx_memories_trust_score ON memories(trust_score);
CREATE INDEX idx_memories_retracted ON memories(retracted_at) WHERE retracted_at IS NOT NULL;
CREATE INDEX idx_memories_validation ON memories(validation_agent_id);
CREATE INDEX idx_memories_id_version ON memories(id, version) WHERE retired_at IS NULL;
CREATE INDEX idx_memories_type ON memories(memory_type);
CREATE INDEX idx_memories_protected ON memories(protected) WHERE protected = 1;
CREATE INDEX idx_memories_gw_broadcast ON memories(gw_broadcast) WHERE gw_broadcast = 1;
CREATE INDEX idx_memories_salience ON memories(salience_score DESC) WHERE retired_at IS NULL;
CREATE INDEX idx_memories_visibility ON memories(visibility);
CREATE INDEX idx_memories_ewc_importance ON memories(ewc_importance DESC) WHERE retired_at IS NULL;
CREATE INDEX idx_memories_alpha ON memories(alpha) WHERE retired_at IS NULL;
CREATE INDEX idx_memories_beta  ON memories(beta)  WHERE retired_at IS NULL;
CREATE INDEX idx_memories_confidence_phase ON memories(agent_id, confidence_phase) WHERE confidence_phase != 0.0;
CREATE INDEX idx_memories_decoherence_rate ON memories(decoherence_rate DESC) WHERE decoherence_rate IS NOT NULL;
CREATE INDEX idx_memories_coherence_syndrome ON memories(agent_id) WHERE coherence_syndrome IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_replay ON memories(replay_priority DESC) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_labile ON memories(labile_until) WHERE labile_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_temporal_level ON memories(temporal_level, agent_id);

CREATE VIEW decoherent_memories AS
            SELECT id, content, confidence, coherence_syndrome, decoherence_rate,
                   temporal_class, created_at, updated_at
            FROM memories
            WHERE coherence_syndrome IS NOT NULL OR decoherence_rate IS NOT NULL
            ORDER BY decoherence_rate DESC;

CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    category,
    tags,
    content=memories,
    content_rowid=id,
    tokenize='porter unicode61'
);

CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories WHEN new.indexed = 1 BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags) VALUES (new.id, new.content, new.category, new.tags);
END;

CREATE TRIGGER memories_fts_update_delete AFTER UPDATE ON memories WHEN old.indexed = 1 BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES ('delete', old.id, old.content, old.category, old.tags);
END;

CREATE TRIGGER memories_fts_update_insert AFTER UPDATE ON memories WHEN new.indexed = 1 AND new.retired_at IS NULL BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags)
    VALUES (new.id, new.content, new.category, new.tags);
END;

CREATE TRIGGER memories_fts_delete AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags) VALUES('delete', old.id, old.content, old.category, old.tags);
END;

CREATE TRIGGER memories_temporal_class_check
BEFORE INSERT ON memories
WHEN NEW.temporal_class NOT IN ('permanent', 'long', 'medium', 'short', 'ephemeral')
BEGIN
    SELECT RAISE(ABORT, 'temporal_class must be one of: permanent, long, medium, short, ephemeral');
END;

CREATE TRIGGER memories_temporal_class_update_check
BEFORE UPDATE OF temporal_class ON memories
WHEN NEW.temporal_class NOT IN ('permanent', 'long', 'medium', 'short', 'ephemeral')
BEGIN
    SELECT RAISE(ABORT, 'temporal_class must be one of: permanent, long, medium, short, ephemeral');
END;

CREATE TRIGGER memories_validate_ts_insert
BEFORE INSERT ON memories
WHEN NEW.created_at NOT LIKE '____-__-__T%'
BEGIN
  SELECT RAISE(ABORT, 'memories.created_at must be ISO 8601 (YYYY-MM-DDTHH:MM:SS)');
END;

CREATE TRIGGER memories_validate_ts_update
BEFORE UPDATE OF created_at ON memories
WHEN NEW.created_at NOT LIKE '____-__-__T%'
BEGIN
  SELECT RAISE(ABORT, 'memories.created_at must be ISO 8601 (YYYY-MM-DDTHH:MM:SS)');
END;

CREATE TRIGGER IF NOT EXISTS trg_agent_delete_nullify_validation
AFTER DELETE ON agents
BEGIN
    UPDATE memories
       SET validation_agent_id = NULL
     WHERE validation_agent_id = OLD.id;
END;

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

CREATE TRIGGER trg_memory_ignition_insert
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

CREATE TRIGGER trg_gw_broadcast_meb
AFTER UPDATE OF gw_broadcast ON memories
WHEN NEW.gw_broadcast = 1 AND OLD.gw_broadcast = 0 AND NEW.retired_at IS NULL
BEGIN
    INSERT INTO memory_events (memory_id, agent_id, operation, category, scope, memory_type, created_at)
    VALUES (
        NEW.id,
        NEW.agent_id,
        'broadcast',
        NEW.category,
        COALESCE(NEW.scope, 'global'),
        COALESCE(NEW.memory_type, 'episodic'),
        strftime('%Y-%m-%dT%H:%M:%S', 'now')
    );
END;

CREATE TRIGGER trg_gw_broadcast_workspace
AFTER UPDATE OF gw_broadcast ON memories
WHEN NEW.gw_broadcast = 1 AND OLD.gw_broadcast = 0 AND NEW.retired_at IS NULL
BEGIN
    INSERT OR IGNORE INTO workspace_broadcasts (memory_id, agent_id, salience, summary, target_scope, triggered_by)
    SELECT
        NEW.id,
        NEW.agent_id,
        NEW.salience_score,
        substr(NEW.content, 1, 200),
        COALESCE(NEW.scope, 'global'),
        'gw_score'
    WHERE NOT EXISTS (
        SELECT 1 FROM workspace_broadcasts wb WHERE wb.memory_id = NEW.id
          AND wb.broadcast_at >= strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-48 hours'))
    );
END;

CREATE TRIGGER memories_visibility_check_insert
BEFORE INSERT ON memories
WHEN NEW.visibility NOT IN ('public', 'project', 'agent', 'restricted')
BEGIN
    SELECT RAISE(ABORT, 'memories.visibility must be one of: public, project, agent, restricted');
END;

CREATE TRIGGER memories_visibility_check_update
BEFORE UPDATE OF visibility ON memories
WHEN NEW.visibility NOT IN ('public', 'project', 'agent', 'restricted')
BEGIN
    SELECT RAISE(ABORT, 'memories.visibility must be one of: public, project, agent, restricted');
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_delete_cascade_edges
AFTER DELETE ON memories
BEGIN
    DELETE FROM knowledge_edges
     WHERE (source_table = 'memories' AND source_id = OLD.id)
        OR (target_table = 'memories' AND target_id = OLD.id);
END;

INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');

CREATE TABLE IF NOT EXISTS procedures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE,
    procedure_key TEXT UNIQUE,
    title TEXT,
    goal TEXT NOT NULL,
    description TEXT,
    task_family TEXT,
    procedure_kind TEXT NOT NULL DEFAULT 'workflow',
    trigger_conditions TEXT,
    preconditions TEXT,
    constraints_json TEXT,
    steps_json TEXT NOT NULL,
    tools_json TEXT,
    failure_modes_json TEXT,
    rollback_steps_json TEXT,
    success_criteria_json TEXT,
    repair_strategies_json TEXT,
    tool_policy_json TEXT,
    expected_outcomes TEXT,
    applicability_scope TEXT NOT NULL DEFAULT 'global',
    temporal_class TEXT DEFAULT 'durable',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','candidate','stale','needs_review','superseded','retired')),
    automation_ready INTEGER NOT NULL DEFAULT 0,
    determinism REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.5,
    utility_score REAL NOT NULL DEFAULT 0.5,
    generality_score REAL NOT NULL DEFAULT 0.5,
    support_count INTEGER NOT NULL DEFAULT 0,
    execution_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    last_executed_at TEXT,
    last_validated_at TEXT,
    stale_after_days INTEGER NOT NULL DEFAULT 90,
    supersedes_procedure_id INTEGER REFERENCES procedures(id),
    retired_at TEXT,
    search_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_procedures_kind ON procedures(procedure_kind);
CREATE INDEX IF NOT EXISTS idx_procedures_status ON procedures(status);
CREATE INDEX IF NOT EXISTS idx_procedures_last_validated ON procedures(last_validated_at);
CREATE INDEX IF NOT EXISTS idx_procedures_execution_count ON procedures(execution_count DESC);
CREATE INDEX IF NOT EXISTS idx_procedures_scope ON procedures(applicability_scope);
CREATE INDEX IF NOT EXISTS idx_procedures_memory_id ON procedures(memory_id);
CREATE INDEX IF NOT EXISTS idx_procedures_supersedes ON procedures(supersedes_procedure_id);

CREATE TABLE IF NOT EXISTS procedure_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL,
    action TEXT NOT NULL,
    rationale TEXT,
    tool_name TEXT,
    expected_output TEXT,
    stop_condition TEXT,
    retry_policy TEXT,
    rollback_hint TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_procedure_steps_procedure_order
ON procedure_steps(procedure_id, step_order);

CREATE TABLE IF NOT EXISTS procedure_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
    memory_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    decision_id INTEGER REFERENCES decisions(id) ON DELETE CASCADE,
    entity_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    source_role TEXT NOT NULL DEFAULT 'evidence',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_procedure_sources_procedure ON procedure_sources(procedure_id);
CREATE INDEX IF NOT EXISTS idx_procedure_sources_memory ON procedure_sources(memory_id);
CREATE INDEX IF NOT EXISTS idx_procedure_sources_event ON procedure_sources(event_id);
CREATE INDEX IF NOT EXISTS idx_procedure_sources_decision ON procedure_sources(decision_id);

CREATE TABLE IF NOT EXISTS procedure_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
    agent_id TEXT REFERENCES agents(id),
    task_family TEXT,
    task_signature TEXT,
    input_summary TEXT,
    outcome_summary TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    usefulness_score REAL,
    errors_seen TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_procedure_runs_procedure_created
ON procedure_runs(procedure_id, created_at DESC);

CREATE TABLE IF NOT EXISTS procedure_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_signature TEXT NOT NULL UNIQUE,
    task_family TEXT,
    normalized_signature TEXT NOT NULL,
    support_count INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT,
    mean_success REAL NOT NULL DEFAULT 0.0,
    promoted_procedure_id INTEGER REFERENCES procedures(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_procedure_candidates_family ON procedure_candidates(task_family);
CREATE INDEX IF NOT EXISTS idx_procedure_candidates_support ON procedure_candidates(support_count DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS procedures_fts USING fts5(
    title,
    goal,
    description,
    task_family,
    search_text,
    content=procedures,
    content_rowid=id,
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS procedures_fts_insert AFTER INSERT ON procedures BEGIN
    INSERT INTO procedures_fts(rowid, title, goal, description, task_family, search_text)
    VALUES (new.id, new.title, new.goal, new.description, new.task_family, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS procedures_fts_update AFTER UPDATE ON procedures BEGIN
    INSERT INTO procedures_fts(procedures_fts, rowid, title, goal, description, task_family, search_text)
    VALUES ('delete', old.id, old.title, old.goal, old.description, old.task_family, old.search_text);
    INSERT INTO procedures_fts(rowid, title, goal, description, task_family, search_text)
    VALUES (new.id, new.title, new.goal, new.description, new.task_family, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS procedures_fts_delete AFTER DELETE ON procedures BEGIN
    INSERT INTO procedures_fts(procedures_fts, rowid, title, goal, description, task_family, search_text)
    VALUES ('delete', old.id, old.title, old.goal, old.description, old.task_family, old.search_text);
END;

COMMIT;
PRAGMA foreign_keys = ON;
