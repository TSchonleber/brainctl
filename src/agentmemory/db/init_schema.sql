-- brainctl init_schema.sql -- Full production schema
-- Generated from brain.db
-- Use: brainctl init

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- Legacy tracking table. Ten migration files still write to this singular
-- form (`INSERT INTO schema_version ...`) for historical reasons. The
-- runner in src/agentmemory/migrate.py uses a separate `schema_versions`
-- (plural) table created lazily via `_ensure_schema_versions()`, which
-- is the authoritative "has this migration been applied?" source. The
-- singular table is preserved so legacy migration statements don't error
-- on fresh installs; nothing reads it. Audit I27 — kept as-is per the
-- "migrations are append-only" convention in CLAUDE.md.
CREATE TABLE schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now')),
    description TEXT
);

CREATE TABLE agents (
    id TEXT PRIMARY KEY,                      -- e.g. 'my-agent', 'data-pipeline', 'reviewer'
    display_name TEXT NOT NULL,
    agent_type TEXT NOT NULL,                 -- 'autonomous', 'pipeline', 'assistant', 'human'
    adapter_info TEXT,                        -- JSON: connection details, model, etc
    status TEXT NOT NULL DEFAULT 'active',    -- active, paused, retired
    last_seen_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    attention_class TEXT NOT NULL DEFAULT 'ic',
    attention_budget_tier INTEGER NOT NULL DEFAULT 1
);

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

-- Split into two triggers so 0→1 promotion correctly adds to FTS without double-delete.
-- Added `NEW.retired_at IS NULL` guard on the INSERT leg so retire UPDATEs
-- (retired_at NULL → non-NULL) do not re-insert the row. The companion
-- trg_memories_fts_purge_on_retire trigger near the end of this file does
-- the actual DELETE at the retire transition; without this guard, the
-- 'delete' command issued there is silently no-op'd by FTS5 statement-level
-- batching against the pending INSERT.
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

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    event_type TEXT NOT NULL,                     -- 'observation', 'result', 'decision', 'error',
                                                   -- 'handoff', 'task_update', 'artifact', 'session_start',
                                                   -- 'session_end', 'memory_promoted', 'memory_retired'
    summary TEXT NOT NULL,
    detail TEXT,                                   -- longer description, stack traces, etc
    metadata TEXT,                                 -- JSON blob for structured data
    session_id TEXT,                               -- links to a specific conversation/run
    project TEXT,                                  -- project context
    refs TEXT,                                     -- JSON array of related entity refs
    importance REAL NOT NULL DEFAULT 0.5,          -- 0.0-1.0 for prioritizing retrieval
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    epoch_id INTEGER REFERENCES epochs(id),
    caused_by_event_id INTEGER REFERENCES events(id),
    causal_chain_root INTEGER REFERENCES events(id)
);

CREATE INDEX idx_events_agent ON events(agent_id);

CREATE INDEX idx_events_type ON events(event_type);

CREATE INDEX idx_events_project ON events(project);

CREATE INDEX idx_events_session ON events(session_id);

CREATE INDEX idx_events_time ON events(created_at DESC);

CREATE INDEX idx_events_importance ON events(importance DESC);

CREATE VIRTUAL TABLE events_fts USING fts5(
    summary,
    detail,
    content=events,
    content_rowid=id,
    tokenize='porter unicode61'
);

CREATE TRIGGER events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, summary, detail) VALUES (new.id, new.summary, new.detail);
END;

CREATE TRIGGER events_fts_update AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, summary, detail) VALUES('delete', old.id, old.summary, old.detail);
    INSERT INTO events_fts(rowid, summary, detail) VALUES (new.id, new.summary, new.detail);
END;

CREATE TRIGGER events_fts_delete AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, summary, detail) VALUES('delete', old.id, old.summary, old.detail);
END;

CREATE TABLE context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,                     -- 'conversation', 'document', 'code', 'skill', 
                                                   -- 'issue', 'pr', 'obsidian_note'
    source_ref TEXT NOT NULL,                      -- URI or path to original
    chunk_index INTEGER NOT NULL DEFAULT 0,        -- for multi-chunk documents
    content TEXT NOT NULL,
    summary TEXT,                                   -- LLM-generated summary of chunk
    project TEXT,
    tags TEXT,                                      -- JSON array
    token_count INTEGER,
    embedding_id INTEGER,                           -- FK to embeddings table (Phase 2)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    stale_at TEXT                                    -- when source was re-indexed
);

CREATE INDEX idx_context_source ON context(source_type, source_ref);

CREATE INDEX idx_context_project ON context(project);

CREATE INDEX idx_context_stale ON context(stale_at) WHERE stale_at IS NULL;

CREATE VIRTUAL TABLE context_fts USING fts5(
    content,
    summary,
    tags,
    content=context,
    content_rowid=id,
    tokenize='porter unicode61'
);

CREATE TRIGGER context_fts_insert AFTER INSERT ON context BEGIN
    INSERT INTO context_fts(rowid, content, summary, tags) VALUES (new.id, new.content, new.summary, new.tags);
END;

CREATE TRIGGER context_fts_update AFTER UPDATE ON context BEGIN
    INSERT INTO context_fts(context_fts, rowid, content, summary, tags) VALUES('delete', old.id, old.content, old.summary, old.tags);
    INSERT INTO context_fts(rowid, content, summary, tags) VALUES (new.id, new.content, new.summary, new.tags);
END;

CREATE TRIGGER context_fts_delete AFTER DELETE ON context BEGIN
    INSERT INTO context_fts(context_fts, rowid, content, summary, tags) VALUES('delete', old.id, old.content, old.summary, old.tags);
END;

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT,                              -- External task ID, GitHub issue #, etc
    external_system TEXT,                           -- 'task-system', 'github', 'manual'
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',         -- pending, in_progress, blocked, completed, cancelled
    priority TEXT NOT NULL DEFAULT 'medium',        -- critical, high, medium, low
    assigned_agent_id TEXT REFERENCES agents(id),
    project TEXT,
    parent_task_id INTEGER REFERENCES tasks(id),
    metadata TEXT,                                  -- JSON: labels, branch name, PR url, etc
    claimed_at TEXT,
    claimed_by TEXT REFERENCES agents(id),
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_tasks_status ON tasks(status);

CREATE INDEX idx_tasks_agent ON tasks(assigned_agent_id);

CREATE INDEX idx_tasks_project ON tasks(project);

CREATE INDEX idx_tasks_external ON tasks(external_system, external_id);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    title TEXT NOT NULL,
    rationale TEXT NOT NULL,
    alternatives_considered TEXT,                   -- JSON array of rejected options
    project TEXT,
    reversible INTEGER NOT NULL DEFAULT 1,         -- boolean
    reversed_at TEXT,
    reversed_by TEXT,
    source_event_id INTEGER REFERENCES events(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_decisions_project ON decisions(project);

CREATE INDEX idx_decisions_agent ON decisions(agent_id);

CREATE TABLE handoff_packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    session_id TEXT,
    chat_id TEXT,
    thread_id TEXT,
    user_id TEXT,
    project TEXT,
    scope TEXT NOT NULL DEFAULT 'global',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'consumed', 'expired', 'pinned')),
    title TEXT,
    goal TEXT NOT NULL,
    current_state TEXT NOT NULL,
    open_loops TEXT NOT NULL,
    next_step TEXT NOT NULL,
    recent_tail TEXT,
    decisions_json TEXT,
    entities_json TEXT,
    tasks_json TEXT,
    facts_json TEXT,
    source_event_id INTEGER REFERENCES events(id),
    consumed_at TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_handoff_status_created ON handoff_packets(status, created_at DESC);

CREATE INDEX idx_handoff_chat_thread_status ON handoff_packets(chat_id, thread_id, status, created_at DESC);

CREATE INDEX idx_handoff_project_status ON handoff_packets(project, status, created_at DESC);

CREATE INDEX idx_handoff_session ON handoff_packets(session_id);

CREATE INDEX idx_handoff_agent_status ON handoff_packets(agent_id, status, created_at DESC);

CREATE TABLE embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,                     -- 'memories', 'context', 'events'
    source_id INTEGER NOT NULL,
    model TEXT NOT NULL,                            -- embedding model used
    dimensions INTEGER NOT NULL,
    vector BLOB,                                    -- raw float32 vector (or use sqlite-vec later)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_embeddings_source ON embeddings(source_table, source_id);

CREATE TABLE agent_state (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,                            -- JSON value
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agent_id, key)
);

CREATE TABLE blobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL,
    disk_path TEXT NOT NULL,                        -- relative path under ~/agentmemory/blobs/
    agent_id TEXT REFERENCES agents(id),
    project TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_blobs_sha256 ON blobs(sha256);

CREATE INDEX idx_blobs_project ON blobs(project);

CREATE TABLE access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,                           -- 'read', 'write', 'search', 'promote', 'retire'
    target_table TEXT,
    target_id INTEGER,
    query TEXT,                                      -- search query if action=search
    result_count INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    tokens_consumed INTEGER,
    task_outcome TEXT
        CHECK (task_outcome IN ('success', 'blocked', 'escalated', 'cancelled')),
    pre_task_uncertainty REAL,
    retrieval_contributed INTEGER DEFAULT NULL
        CHECK (retrieval_contributed IN (0, 1, NULL)),
    task_id TEXT
);

CREATE INDEX idx_access_agent ON access_log(agent_id);

CREATE INDEX idx_access_time ON access_log(created_at DESC);

CREATE TABLE epochs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    parent_epoch_id INTEGER REFERENCES epochs(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_epochs_started ON epochs(started_at);

CREATE INDEX idx_epochs_parent ON epochs(parent_epoch_id);

CREATE INDEX idx_memories_epoch ON memories(epoch_id);

CREATE INDEX idx_memories_temporal_class ON memories(temporal_class);

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

CREATE INDEX idx_events_epoch ON events(epoch_id);

CREATE INDEX idx_events_caused_by ON events(caused_by_event_id);

CREATE INDEX idx_events_causal_root ON events(causal_chain_root);

CREATE TABLE knowledge_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    target_table TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    agent_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_reinforced_at TEXT,
    co_activation_count INTEGER DEFAULT 0,
    weight_updated_at TEXT,
    CHECK (weight >= 0.0 AND weight <= 1.0)
);

CREATE UNIQUE INDEX uq_knowledge_edges_relation
ON knowledge_edges (source_table, source_id, target_table, target_id, relation_type);

CREATE INDEX idx_knowledge_edges_source_pair
ON knowledge_edges (source_table, source_id);

CREATE INDEX idx_knowledge_edges_target_pair
ON knowledge_edges (target_table, target_id);

CREATE INDEX idx_knowledge_edges_relation_type
ON knowledge_edges (relation_type);

CREATE TABLE memory_trust_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    category TEXT NOT NULL,
    trust_score REAL NOT NULL DEFAULT 1.0 CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    sample_count INTEGER NOT NULL DEFAULT 0,      -- number of memories evaluated
    validated_count INTEGER NOT NULL DEFAULT 0,    -- number that passed validation
    retracted_count INTEGER NOT NULL DEFAULT 0,    -- number retracted (lowers trust)
    last_evaluated_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, category)
);

CREATE INDEX idx_trust_scores_agent ON memory_trust_scores(agent_id);

CREATE INDEX idx_trust_scores_category ON memory_trust_scores(category);

CREATE INDEX idx_trust_scores_score ON memory_trust_scores(trust_score);

CREATE INDEX idx_memories_trust_score ON memories(trust_score);

CREATE INDEX idx_memories_retracted ON memories(retracted_at) WHERE retracted_at IS NOT NULL;

CREATE INDEX idx_memories_validation ON memories(validation_agent_id);

CREATE INDEX idx_memories_id_version ON memories(id, version) WHERE retired_at IS NULL;

CREATE INDEX idx_memories_type ON memories(memory_type);

CREATE TABLE situation_models (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    name            TEXT NOT NULL UNIQUE,
    query_anchor    TEXT NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_event_id   INTEGER,
    last_memory_id  TEXT,
    coherence_score REAL DEFAULT 0.0,
    completeness    REAL DEFAULT 0.0,
    status          TEXT DEFAULT 'active'
                    CHECK (status IN ('active','stale','contradictory','archived')),
    narrative       TEXT,
    structured      TEXT,
    ttl_seconds     INTEGER DEFAULT 21600,
    source_memory_ids TEXT DEFAULT '[]',
    source_event_ids  TEXT DEFAULT '[]'
);

CREATE TABLE situation_model_contradictions (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    model_id        TEXT NOT NULL REFERENCES situation_models(id) ON DELETE CASCADE,
    memory_id_a     TEXT,
    memory_id_b     TEXT,
    contradiction   TEXT NOT NULL,
    resolution      TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_sm_anchor ON situation_models(query_anchor);

CREATE INDEX idx_sm_status ON situation_models(status);

CREATE INDEX idx_sm_updated ON situation_models(updated_at);

CREATE TRIGGER events_validate_ts_insert
BEFORE INSERT ON events
WHEN NEW.created_at NOT LIKE '____-__-__T%'
BEGIN
  SELECT RAISE(ABORT, 'events.created_at must be ISO 8601 (YYYY-MM-DDTHH:MM:SS)');
END;

CREATE TRIGGER events_validate_ts_update
BEFORE UPDATE OF created_at ON events
WHEN NEW.created_at NOT LIKE '____-__-__T%'
BEGIN
  SELECT RAISE(ABORT, 'events.created_at must be ISO 8601 (YYYY-MM-DDTHH:MM:SS)');
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

CREATE TABLE knowledge_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,                        -- 'agent:X', 'project:Y', 'global', 'topic:Z'
    memory_count INTEGER NOT NULL DEFAULT 0,
    avg_confidence REAL,
    min_confidence REAL,
    max_confidence REAL,
    freshest_memory_at TEXT,                    -- ISO 8601 datetime of newest active memory in scope
    stalest_memory_at TEXT,                     -- ISO 8601 datetime of oldest active memory in scope
    coverage_density REAL,                      -- composite: count × avg_confidence × recency_factor
    last_computed_at TEXT NOT NULL,
    UNIQUE(scope)
);

CREATE INDEX idx_coverage_scope ON knowledge_coverage(scope);

CREATE INDEX idx_coverage_density ON knowledge_coverage(coverage_density DESC);

CREATE TABLE knowledge_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_type TEXT NOT NULL CHECK(gap_type IN (
        'coverage_hole',         -- no memories in scope at all
        'staleness_hole',        -- memories exist but all too old
        'confidence_hole',       -- memories exist but avg confidence too low
        'contradiction_hole',    -- memories contradict each other
        -- Migration 036 self-healing scan types
        'orphan_memory',         -- memory with no edges + no recalls + old
        'broken_edge',           -- knowledge_edges row points at deleted row
        'unreferenced_entity'    -- entity with nothing linking to it
    )),
    scope TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    triggered_by TEXT,                          -- query or scan that revealed the gap
    severity REAL NOT NULL DEFAULT 0.5          -- 0.0–1.0
        CHECK(severity >= 0.0 AND severity <= 1.0),
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE INDEX idx_gaps_scope ON knowledge_gaps(scope);

CREATE INDEX idx_gaps_type ON knowledge_gaps(gap_type);

CREATE INDEX idx_gaps_unresolved ON knowledge_gaps(resolved_at) WHERE resolved_at IS NULL;

CREATE INDEX idx_gaps_severity ON knowledge_gaps(severity DESC) WHERE resolved_at IS NULL;

CREATE TABLE reflexion_lessons (
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

    -- Generalization scope (JSON array: "agent_type:pipeline", "capability:search", etc.)
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
    retirement_reason TEXT,
    propagated_to TEXT NOT NULL DEFAULT '[]',
    propagation_source_lesson_id INTEGER REFERENCES reflexion_lessons(id)
);

CREATE INDEX idx_rlessons_agent
    ON reflexion_lessons(source_agent_id);

CREATE INDEX idx_rlessons_failure_class
    ON reflexion_lessons(failure_class);

CREATE INDEX idx_rlessons_status
    ON reflexion_lessons(status) WHERE status = 'active';

CREATE INDEX idx_rlessons_confidence
    ON reflexion_lessons(confidence DESC);

CREATE INDEX idx_rlessons_generalizable
    ON reflexion_lessons(generalizable_to);

CREATE INDEX idx_rlessons_active_class
    ON reflexion_lessons(status, failure_class, confidence DESC)
    WHERE status = 'active';

CREATE VIRTUAL TABLE reflexion_lessons_fts USING fts5(
    trigger_conditions,
    lesson_content,
    failure_class,
    failure_subclass,
    content=reflexion_lessons,
    content_rowid=id,
    tokenize='porter unicode61'
);

CREATE TRIGGER rlessons_fts_insert AFTER INSERT ON reflexion_lessons BEGIN
    INSERT INTO reflexion_lessons_fts(rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES (new.id, new.trigger_conditions, new.lesson_content, new.failure_class, new.failure_subclass);
END;

CREATE TRIGGER rlessons_fts_update AFTER UPDATE ON reflexion_lessons BEGIN
    INSERT INTO reflexion_lessons_fts(reflexion_lessons_fts, rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES ('delete', old.id, old.trigger_conditions, old.lesson_content, old.failure_class, old.failure_subclass);
    INSERT INTO reflexion_lessons_fts(rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES (new.id, new.trigger_conditions, new.lesson_content, new.failure_class, new.failure_subclass);
END;

CREATE TRIGGER rlessons_fts_delete AFTER DELETE ON reflexion_lessons BEGIN
    INSERT INTO reflexion_lessons_fts(reflexion_lessons_fts, rowid, trigger_conditions, lesson_content, failure_class, failure_subclass)
    VALUES ('delete', old.id, old.trigger_conditions, old.lesson_content, old.failure_class, old.failure_subclass);
END;

CREATE TRIGGER rlessons_updated_at AFTER UPDATE ON reflexion_lessons BEGIN
    UPDATE reflexion_lessons SET updated_at = datetime('now') WHERE id = new.id;
END;

CREATE TABLE agent_expertise (
            agent_id       TEXT NOT NULL REFERENCES agents(id),
            domain         TEXT NOT NULL,
            strength       REAL NOT NULL DEFAULT 0.0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            last_active    TEXT,
            updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
            brier_score    REAL DEFAULT NULL,
            PRIMARY KEY (agent_id, domain)
        );

CREATE INDEX idx_expertise_domain ON agent_expertise(domain);

CREATE INDEX idx_expertise_strength ON agent_expertise(strength DESC);

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

CREATE INDEX idx_meb_id_asc     ON memory_events(id ASC);

CREATE INDEX idx_meb_agent      ON memory_events(agent_id);

CREATE INDEX idx_meb_category   ON memory_events(category);

CREATE INDEX idx_meb_scope      ON memory_events(scope);

CREATE INDEX idx_meb_created_at ON memory_events(created_at DESC);

CREATE INDEX idx_meb_ttl        ON memory_events(ttl_expires_at)
    WHERE ttl_expires_at IS NOT NULL;

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

CREATE TABLE meb_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE TABLE policy_memories (
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

CREATE INDEX idx_pm_status_category ON policy_memories(status, category);

CREATE INDEX idx_pm_scope ON policy_memories(scope);

CREATE INDEX idx_pm_confidence ON policy_memories(confidence_threshold DESC);

CREATE INDEX idx_pm_priority ON policy_memories(priority DESC);

CREATE INDEX idx_pm_expires ON policy_memories(expires_at) WHERE expires_at IS NOT NULL;

CREATE INDEX idx_pm_authored_by ON policy_memories(authored_by);

CREATE VIRTUAL TABLE policy_memories_fts USING fts5(
    trigger_condition,
    action_directive,
    name,
    content=policy_memories,
    content_rowid=rowid
);

CREATE TRIGGER pm_fts_insert AFTER INSERT ON policy_memories BEGIN
    INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
    VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
END;

CREATE TRIGGER pm_fts_update AFTER UPDATE ON policy_memories BEGIN
    INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
    VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
    INSERT INTO policy_memories_fts(rowid, trigger_condition, action_directive, name)
    VALUES (new.rowid, new.trigger_condition, new.action_directive, new.name);
END;

CREATE TRIGGER pm_fts_delete AFTER DELETE ON policy_memories BEGIN
    INSERT INTO policy_memories_fts(policy_memories_fts, rowid, trigger_condition, action_directive, name)
    VALUES ('delete', old.rowid, old.trigger_condition, old.action_directive, old.name);
END;

CREATE TABLE procedures (
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

CREATE INDEX idx_procedures_kind ON procedures(procedure_kind);

CREATE INDEX idx_procedures_status ON procedures(status);

CREATE INDEX idx_procedures_last_validated ON procedures(last_validated_at);

CREATE INDEX idx_procedures_execution_count ON procedures(execution_count DESC);

CREATE INDEX idx_procedures_scope ON procedures(applicability_scope);

CREATE INDEX idx_procedures_memory_id ON procedures(memory_id);

CREATE INDEX idx_procedures_supersedes ON procedures(supersedes_procedure_id);

CREATE TABLE procedure_steps (
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

CREATE INDEX idx_procedure_steps_procedure_order
ON procedure_steps(procedure_id, step_order);

CREATE TABLE procedure_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
    memory_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    decision_id INTEGER REFERENCES decisions(id) ON DELETE CASCADE,
    entity_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    source_role TEXT NOT NULL DEFAULT 'evidence',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_procedure_sources_procedure ON procedure_sources(procedure_id);

CREATE INDEX idx_procedure_sources_memory ON procedure_sources(memory_id);

CREATE INDEX idx_procedure_sources_event ON procedure_sources(event_id);

CREATE INDEX idx_procedure_sources_decision ON procedure_sources(decision_id);

CREATE TABLE procedure_runs (
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

CREATE INDEX idx_procedure_runs_procedure_created
ON procedure_runs(procedure_id, created_at DESC);

CREATE TABLE procedure_candidates (
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

CREATE INDEX idx_procedure_candidates_family ON procedure_candidates(task_family);

CREATE INDEX idx_procedure_candidates_support ON procedure_candidates(support_count DESC);

CREATE VIRTUAL TABLE procedures_fts USING fts5(
    title,
    goal,
    description,
    task_family,
    search_text,
    content=procedures,
    content_rowid=id,
    tokenize='porter unicode61'
);

CREATE TRIGGER procedures_fts_insert AFTER INSERT ON procedures BEGIN
    INSERT INTO procedures_fts(rowid, title, goal, description, task_family, search_text)
    VALUES (new.id, new.title, new.goal, new.description, new.task_family, new.search_text);
END;

CREATE TRIGGER procedures_fts_update AFTER UPDATE ON procedures BEGIN
    INSERT INTO procedures_fts(procedures_fts, rowid, title, goal, description, task_family, search_text)
    VALUES ('delete', old.id, old.title, old.goal, old.description, old.task_family, old.search_text);
    INSERT INTO procedures_fts(rowid, title, goal, description, task_family, search_text)
    VALUES (new.id, new.title, new.goal, new.description, new.task_family, new.search_text);
END;

CREATE TRIGGER procedures_fts_delete AFTER DELETE ON procedures BEGIN
    INSERT INTO procedures_fts(procedures_fts, rowid, title, goal, description, task_family, search_text)
    VALUES ('delete', old.id, old.title, old.goal, old.description, old.task_family, old.search_text);
END;

CREATE TABLE agent_beliefs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id            TEXT    NOT NULL REFERENCES agents(id),
    topic               TEXT    NOT NULL,
        -- Scoped topic key, e.g.:
        --   "project:agentmemory:status"
        --   "agent:my-agent:role"
        --   "global:memory_spine:schema_version"
        --   "task:internal-ref:status"
    belief_content      TEXT    NOT NULL,
    confidence          REAL    NOT NULL DEFAULT 1.0
                            CHECK(confidence >= 0.0 AND confidence <= 1.0),
    source_memory_id    INTEGER REFERENCES memories(id),
    source_event_id     INTEGER REFERENCES events(id),
    is_assumption       INTEGER NOT NULL DEFAULT 0,
        -- 1 = unverified assumption (agent inferred, not explicitly told)
        -- 0 = derived from direct evidence or memory injection
    last_updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    invalidated_at      TEXT,               -- NULL = still believed / active
    invalidation_reason TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    is_superposed       INTEGER DEFAULT 0,
    belief_density_matrix BLOB DEFAULT NULL,
    coherence_score     REAL DEFAULT 0.0,
    entanglement_source_ids TEXT DEFAULT NULL,
    UNIQUE(agent_id, topic)
);

CREATE INDEX idx_beliefs_agent      ON agent_beliefs(agent_id);

CREATE INDEX idx_beliefs_topic      ON agent_beliefs(topic);

CREATE INDEX idx_beliefs_active     ON agent_beliefs(invalidated_at) WHERE invalidated_at IS NULL;

CREATE INDEX idx_beliefs_assumption ON agent_beliefs(is_assumption) WHERE is_assumption = 1;

CREATE INDEX idx_beliefs_stale      ON agent_beliefs(last_updated_at);

CREATE TABLE belief_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT    NOT NULL,
    agent_a_id      TEXT    NOT NULL REFERENCES agents(id),
    agent_b_id      TEXT    REFERENCES agents(id),
        -- NULL = conflict is with global ground truth (memories), not another agent
    belief_a        TEXT    NOT NULL,   -- what agent A believes
    belief_b        TEXT    NOT NULL,   -- what agent B believes, or ground truth
    conflict_type   TEXT    NOT NULL DEFAULT 'factual'
        CHECK(conflict_type IN (
            'factual',      -- two agents disagree on a fact
            'assumption',   -- one agent is acting on an unverified assumption
            'staleness',    -- one agent's belief is outdated vs. current ground truth
            'scope'         -- agents disagree about ownership or responsibility
        )),
    severity        REAL    NOT NULL DEFAULT 0.5
        CHECK(severity >= 0.0 AND severity <= 1.0),
    detected_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    resolved_at     TEXT,
    resolution      TEXT,
    requires_supervisor_intervention INTEGER NOT NULL DEFAULT 0
        -- 1 = supervisor agent should inject corrective context before affected agents act
);

CREATE INDEX idx_conflicts_topic    ON belief_conflicts(topic);

CREATE INDEX idx_conflicts_agent_a  ON belief_conflicts(agent_a_id);

CREATE INDEX idx_conflicts_agent_b  ON belief_conflicts(agent_b_id);

CREATE INDEX idx_conflicts_open     ON belief_conflicts(resolved_at) WHERE resolved_at IS NULL;

CREATE INDEX idx_conflicts_severity ON belief_conflicts(severity DESC) WHERE resolved_at IS NULL;

CREATE INDEX idx_conflicts_supervisor ON belief_conflicts(requires_supervisor_intervention)
    WHERE requires_supervisor_intervention = 1 AND resolved_at IS NULL;

CREATE TABLE agent_perspective_models (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    observer_agent_id       TEXT    NOT NULL REFERENCES agents(id),
    subject_agent_id        TEXT    NOT NULL REFERENCES agents(id),
    topic                   TEXT    NOT NULL,
    estimated_belief        TEXT,
        -- Observer's best estimate of what subject currently believes.
        -- NULL = observer has no model for this topic (treat as full gap).
    estimated_confidence    REAL
        CHECK(estimated_confidence IS NULL OR (estimated_confidence >= 0.0 AND estimated_confidence <= 1.0)),
        -- How confident is the observer in their estimate of subject's belief?
    knowledge_gap           TEXT,
        -- What observer believes subject does NOT know about this topic.
        -- This is the delta to fill when routing context to subject.
        -- NULL = no known gap (subject likely has sufficient context).
    confusion_risk          REAL    NOT NULL DEFAULT 0.0
        CHECK(confusion_risk >= 0.0 AND confusion_risk <= 1.0),
        -- Probability subject will be confused or err on tasks requiring
        -- knowledge of this topic. Supervisor uses this for proactive injection.
        -- Thresholds: > 0.7 = HIGH (inject before routing), 0.4–0.7 = MODERATE
    last_updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(observer_agent_id, subject_agent_id, topic)
);

CREATE INDEX idx_pmodel_observer  ON agent_perspective_models(observer_agent_id);

CREATE INDEX idx_pmodel_subject   ON agent_perspective_models(subject_agent_id);

CREATE INDEX idx_pmodel_topic     ON agent_perspective_models(topic);

CREATE INDEX idx_pmodel_confusion ON agent_perspective_models(confusion_risk DESC);

CREATE INDEX idx_pmodel_gaps      ON agent_perspective_models(knowledge_gap)
    WHERE knowledge_gap IS NOT NULL;

CREATE TABLE agent_bdi_state (
    agent_id                    TEXT    PRIMARY KEY REFERENCES agents(id),

    -- BELIEFS dimension
    beliefs_summary             TEXT,
        -- JSON: {
        --   "active_belief_count": N,
        --   "stale_belief_count": N,       (last_updated > 24h for active-task topics)
        --   "assumption_count": N,          (is_assumption = 1)
        --   "conflict_count": N,            (open belief_conflicts for this agent)
        --   "key_topics": ["t1", "t2", ...]
        -- }
    beliefs_last_updated_at     TEXT,

    -- DESIRES dimension
    desires_summary             TEXT,
        -- JSON: {
        --   "active_task_count": N,
        --   "primary_goal": "...",
        --   "priority": "critical|high|medium|low",
        --   "task_ids": ["internal-ref", ...]
        -- }
    desires_last_updated_at     TEXT,

    -- INTENTIONS dimension
    intentions_summary          TEXT,
        -- JSON: {
        --   "in_progress_tasks": [...],
        --   "committed_actions": [...],    (from recent events)
        --   "estimated_completion": "..."
        -- }
    intentions_last_updated_at  TEXT,

    -- EPISTEMIC HEALTH SCORES (0.0–1.0)
    knowledge_coverage_score    REAL,
        -- How well does agent's belief state cover topics required
        -- by their current active tasks? 1.0 = full coverage.
    belief_staleness_score      REAL,
        -- Fraction of active-task beliefs that are stale (>24h).
        -- 1.0 = all beliefs are stale. Target < 0.2.
    confusion_risk_score        REAL,
        -- Aggregate max confusion_risk from agent_perspective_models
        -- where this agent is the subject. 1.0 = high confusion expected.
        -- Supervisor triggers proactive injection when this > 0.7.

    last_full_assessment_at     TEXT,
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX idx_bdi_coverage  ON agent_bdi_state(knowledge_coverage_score);

CREATE INDEX idx_bdi_staleness ON agent_bdi_state(belief_staleness_score DESC);

CREATE INDEX idx_bdi_confusion ON agent_bdi_state(confusion_risk_score DESC);

CREATE TABLE neuromodulation_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    org_state TEXT NOT NULL DEFAULT 'normal'
        CHECK(org_state IN ('normal', 'incident', 'sprint', 'strategic_planning', 'focused_work')),
    dopamine_signal        REAL NOT NULL DEFAULT 0.0,
    confidence_boost_rate  REAL NOT NULL DEFAULT 0.10,
    confidence_decay_rate  REAL NOT NULL DEFAULT 0.02,
    dopamine_last_fired_at TEXT,
    arousal_level                REAL NOT NULL DEFAULT 0.3,
    retrieval_breadth_multiplier REAL NOT NULL DEFAULT 1.0,
    consolidation_immediacy      TEXT NOT NULL DEFAULT 'scheduled'
                                     CHECK(consolidation_immediacy IN ('immediate', 'scheduled')),
    consolidation_interval_mins  INTEGER NOT NULL DEFAULT 240,
    focus_level                REAL NOT NULL DEFAULT 0.3,
    similarity_threshold_delta REAL NOT NULL DEFAULT 0.0,
    scope_restriction          TEXT,
    exploitation_bias          REAL NOT NULL DEFAULT 0.0,
    temporal_lambda       REAL NOT NULL DEFAULT 0.030,
    context_window_depth  INTEGER NOT NULL DEFAULT 50,
    detected_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    detection_method TEXT NOT NULL DEFAULT 'auto'
                         CHECK(detection_method IN ('auto', 'manual', 'policy')),
    expires_at       TEXT,
    triggered_by     TEXT,
    notes            TEXT
);

CREATE UNIQUE INDEX idx_neuromod_singleton ON neuromodulation_state(id);

CREATE TABLE neuromodulation_transitions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_state       TEXT NOT NULL,
    to_state         TEXT NOT NULL,
    reason           TEXT,
    triggered_by     TEXT,
    transitioned_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX idx_neuromod_transitions_ts ON neuromodulation_transitions(transitioned_at DESC);

CREATE INDEX idx_memories_protected ON memories(protected) WHERE protected = 1;

CREATE TABLE dream_hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_a_id INTEGER NOT NULL REFERENCES memories(id),
    memory_b_id INTEGER NOT NULL REFERENCES memories(id),
    hypothesis_memory_id INTEGER REFERENCES memories(id),  -- the synthesized hypothesis memory
    similarity REAL NOT NULL,                              -- cosine similarity at creation time
    status TEXT NOT NULL DEFAULT 'incubating'              -- incubating | promoted | retired
        CHECK(status IN ('incubating', 'promoted', 'retired')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    promoted_at TEXT,
    retired_at TEXT,
    retirement_reason TEXT
);

CREATE INDEX idx_dream_hypotheses_status ON dream_hypotheses(status);

CREATE INDEX idx_dream_hypotheses_created ON dream_hypotheses(created_at DESC);

CREATE INDEX idx_dream_hypotheses_hypothesis_memory ON dream_hypotheses(hypothesis_memory_id);

CREATE INDEX idx_dream_hypotheses_pair ON dream_hypotheses(memory_a_id, memory_b_id);

CREATE TABLE workspace_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE TABLE workspace_broadcasts (
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

CREATE INDEX idx_wb_broadcast_at   ON workspace_broadcasts(broadcast_at DESC);

CREATE INDEX idx_wb_memory_id      ON workspace_broadcasts(memory_id);

CREATE INDEX idx_wb_agent_id       ON workspace_broadcasts(agent_id);

CREATE INDEX idx_wb_target_scope   ON workspace_broadcasts(target_scope);

CREATE INDEX idx_wb_expires        ON workspace_broadcasts(expires_at);

CREATE TABLE workspace_acks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcast_id   INTEGER NOT NULL REFERENCES workspace_broadcasts(id),
    agent_id       TEXT    NOT NULL,
    acked_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(broadcast_id, agent_id)
);

CREATE INDEX idx_wacks_broadcast ON workspace_acks(broadcast_id);

CREATE INDEX idx_wacks_agent     ON workspace_acks(agent_id);

CREATE TRIGGER trg_ws_ack_count
AFTER INSERT ON workspace_acks
BEGIN
    UPDATE workspace_broadcasts
       SET ack_count = ack_count + 1
     WHERE id = NEW.broadcast_id;
END;

CREATE TABLE workspace_phi (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start     TEXT NOT NULL,
    window_end       TEXT NOT NULL,
    phi_org          REAL NOT NULL DEFAULT 0.0,   -- mean pair-wise integration
    broadcast_count  INTEGER NOT NULL DEFAULT 0,  -- broadcasts in window
    ack_rate         REAL NOT NULL DEFAULT 0.0,   -- fraction of broadcasts acked
    agent_pair_count INTEGER NOT NULL DEFAULT 0,  -- active agent pairs counted
    computed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX idx_wphi_window ON workspace_phi(window_end DESC);

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

CREATE TABLE agent_capabilities (
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    capability      TEXT NOT NULL,          -- e.g. "sql_migration", "research", "memory_ops"
    skill_level     REAL NOT NULL DEFAULT 0.5,   -- 0.0-1.0 estimated proficiency
    task_count      INTEGER NOT NULL DEFAULT 0,  -- result events logged in this domain
    avg_events      REAL,                    -- avg events per task burst (proxy for effort)
    block_rate      REAL DEFAULT 0.0,        -- fraction of events that were blocked/errors
    last_active     TEXT,                    -- last event timestamp in this domain
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (agent_id, capability)
);

CREATE INDEX idx_agent_caps_agent ON agent_capabilities(agent_id);

CREATE INDEX idx_agent_caps_cap ON agent_capabilities(capability);

CREATE INDEX idx_agent_caps_skill ON agent_capabilities(skill_level DESC);

CREATE TABLE world_model_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_type    TEXT NOT NULL,          -- 'org_state' | 'prediction' | 'error_log'
    subject_id       TEXT,                   -- agent_id, project name, or task ref
    subject_type     TEXT,                   -- 'agent' | 'project' | 'task'
    predicted_state  TEXT,                   -- JSON: the predicted state
    actual_state     TEXT,                   -- JSON: filled in after resolution
    prediction_error REAL,                   -- scalar distance |predicted - actual| (0.0-1.0)
    author_agent_id  TEXT REFERENCES agents(id),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    resolved_at      TEXT
);

CREATE INDEX idx_wm_snapshots_type ON world_model_snapshots(snapshot_type);

CREATE INDEX idx_wm_snapshots_subject ON world_model_snapshots(subject_id);

CREATE INDEX idx_wm_snapshots_unresolved ON world_model_snapshots(resolved_at) WHERE resolved_at IS NULL;

CREATE TABLE deferred_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,                       -- who issued the original search
    query_text TEXT NOT NULL,                     -- the raw search query
    query_embedding BLOB,                         -- optional: embedding vector for vec retry
    queried_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,                              -- NULL = 30-day default applied at retry
    resolved_at TEXT,                             -- NULL while still pending
    resolution_memory_id INTEGER REFERENCES memories(id),
    attempts INTEGER NOT NULL DEFAULT 0           -- retry counter
);

CREATE INDEX idx_deferred_queries_agent    ON deferred_queries(agent_id);

CREATE INDEX idx_deferred_queries_pending  ON deferred_queries(resolved_at) WHERE resolved_at IS NULL;

CREATE INDEX idx_deferred_queries_queried  ON deferred_queries(queried_at DESC);

CREATE TABLE neuro_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_state TEXT NOT NULL,
    dopamine_level REAL NOT NULL DEFAULT 0.0,
    norepinephrine_level REAL NOT NULL DEFAULT 0.0,
    acetylcholine_level REAL NOT NULL DEFAULT 0.0,
    serotonin_level REAL NOT NULL DEFAULT 0.3,
    computed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    source TEXT NOT NULL DEFAULT 'auto_detect',
    agent_id TEXT,
    notes TEXT
);

CREATE INDEX idx_neuro_events_time ON neuro_events(computed_at);

CREATE INDEX idx_memories_gw_broadcast ON memories(gw_broadcast) WHERE gw_broadcast = 1;

CREATE INDEX idx_memories_salience ON memories(salience_score DESC) WHERE retired_at IS NULL;

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

CREATE INDEX idx_memories_visibility ON memories(visibility);

CREATE INDEX idx_memories_ewc_importance ON memories(ewc_importance DESC) WHERE retired_at IS NULL;

CREATE TABLE world_model (
            entity_id        TEXT NOT NULL PRIMARY KEY,
            entity_type      TEXT CHECK(entity_type IN ('agent', 'project', 'goal', 'dependency')),
            state_snapshot   TEXT NOT NULL,
            causal_parents   TEXT,
            last_synced_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        );

CREATE INDEX idx_world_model_type ON world_model(entity_type);

CREATE INDEX idx_rlessons_propagated ON reflexion_lessons(propagated_to)
    WHERE propagated_to != '[]';

CREATE INDEX idx_rlessons_prop_source ON reflexion_lessons(propagation_source_lesson_id)
    WHERE propagation_source_lesson_id IS NOT NULL;

CREATE INDEX idx_memories_alpha ON memories(alpha) WHERE retired_at IS NULL;

CREATE INDEX idx_memories_beta  ON memories(beta)  WHERE retired_at IS NULL;

CREATE TABLE agent_uncertainty_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    task_desc       TEXT,                                    -- task description that triggered the scan
    gap_topic       TEXT,                                    -- what the agent didn't know
    free_energy     REAL,                                    -- (1 - confidence) * importance at scan time
    resolved_at     TIMESTAMP,                               -- when the gap was filled
    resolved_by     INTEGER REFERENCES memories(id),         -- memory that resolved the gap
    propagated      BOOLEAN DEFAULT FALSE,                   -- whether gap was propagated to other agents
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    domain          TEXT,
    query           TEXT,
    result_count    INTEGER,
    avg_confidence  REAL,
    retrieved_at    DATETIME DEFAULT (datetime('now')),
    temporal_class  TEXT     DEFAULT 'ephemeral',
    ttl_days        INTEGER  DEFAULT 30
);

CREATE INDEX idx_unc_agent     ON agent_uncertainty_log(agent_id);

CREATE INDEX idx_unc_created   ON agent_uncertainty_log(created_at);

CREATE INDEX idx_unc_resolved  ON agent_uncertainty_log(resolved_at);

CREATE INDEX idx_unc_task      ON agent_uncertainty_log(agent_id, resolved_at);

CREATE INDEX idx_expertise_brier ON agent_expertise(brier_score) WHERE brier_score IS NOT NULL;

CREATE INDEX idx_unc_domain     ON agent_uncertainty_log(domain);

CREATE INDEX idx_unc_retrieved  ON agent_uncertainty_log(retrieved_at);

CREATE INDEX idx_access_agent_day
    ON access_log(agent_id, created_at DESC);

CREATE TABLE entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                            -- unique human-readable identifier
    entity_type TEXT NOT NULL,                     -- 'person', 'organization', 'project', 'tool', 'concept', 'agent', 'location', 'event', 'document'
    properties TEXT NOT NULL DEFAULT '{}',         -- JSON object of typed properties
    observations TEXT NOT NULL DEFAULT '[]',       -- JSON array of atomic fact strings
    agent_id TEXT NOT NULL REFERENCES agents(id),  -- who created this entity
    confidence REAL NOT NULL DEFAULT 1.0,          -- 0.0-1.0
    scope TEXT NOT NULL DEFAULT 'global',          -- 'global', 'project:<name>', 'agent:<id>'
    retired_at TEXT,                               -- soft delete
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Migration 033: compiled-truth synthesis surface
    compiled_truth TEXT,
    compiled_truth_updated_at TEXT,
    compiled_truth_source TEXT,
    -- Migration 034: enrichment tier (T1 critical / T2 notable / T3 minor)
    enrichment_tier INTEGER NOT NULL DEFAULT 3,
    last_enriched_at TEXT,
    -- Migration 035: aliases JSON list for canonical-name dedup
    aliases TEXT
);

CREATE UNIQUE INDEX uq_entities_name_scope ON entities(name, scope) WHERE retired_at IS NULL;

CREATE INDEX idx_entities_type ON entities(entity_type);

CREATE INDEX idx_entities_agent ON entities(agent_id);

CREATE INDEX idx_entities_scope ON entities(scope);

CREATE INDEX idx_entities_active ON entities(retired_at) WHERE retired_at IS NULL;

CREATE INDEX idx_entities_compiled_truth_updated_at ON entities(compiled_truth_updated_at);

CREATE INDEX idx_entities_tier_enriched ON entities(enrichment_tier, last_enriched_at)
    WHERE retired_at IS NULL AND enrichment_tier < 3;

CREATE VIRTUAL TABLE entities_fts USING fts5(
    name,
    entity_type,
    properties,
    observations,
    content=entities,
    content_rowid=id,
    tokenize='unicode61'
);

CREATE TRIGGER entities_fts_insert AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts(rowid, name, entity_type, properties, observations)
    VALUES (new.id, new.name, new.entity_type, new.properties, new.observations);
END;

CREATE TRIGGER entities_fts_update AFTER UPDATE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, entity_type, properties, observations)
    VALUES('delete', old.id, old.name, old.entity_type, old.properties, old.observations);
    INSERT INTO entities_fts(rowid, name, entity_type, properties, observations)
    VALUES (new.id, new.name, new.entity_type, new.properties, new.observations);
END;

CREATE TRIGGER entities_fts_delete AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, entity_type, properties, observations)
    VALUES('delete', old.id, old.name, old.entity_type, old.properties, old.observations);
END;

CREATE INDEX idx_memories_confidence_phase ON memories(agent_id, confidence_phase) WHERE confidence_phase != 0.0;

CREATE INDEX idx_memories_decoherence_rate ON memories(decoherence_rate DESC) WHERE decoherence_rate IS NOT NULL;

CREATE INDEX idx_memories_coherence_syndrome ON memories(agent_id) WHERE coherence_syndrome IS NOT NULL;

CREATE INDEX idx_agent_beliefs_superposed ON agent_beliefs(agent_id, is_superposed) WHERE is_superposed = 1;

CREATE INDEX idx_agent_beliefs_coherence ON agent_beliefs(agent_id, coherence_score DESC) WHERE is_superposed = 1;

CREATE INDEX idx_agent_beliefs_entanglement_sources ON agent_beliefs(agent_id) WHERE entanglement_source_ids IS NOT NULL;

CREATE VIEW superposed_beliefs AS
            SELECT ab.id, ab.agent_id, ab.topic, ab.is_superposed,
                   ab.coherence_score, ab.entanglement_source_ids,
                   ab.created_at, ab.updated_at
            FROM agent_beliefs ab WHERE ab.is_superposed = 1;

CREATE VIEW decoherent_memories AS
            SELECT id, content, confidence, coherence_syndrome, decoherence_rate,
                   temporal_class, created_at, updated_at
            FROM memories
            WHERE coherence_syndrome IS NOT NULL OR decoherence_rate IS NOT NULL
            ORDER BY decoherence_rate DESC;

CREATE VIEW recent_belief_collapses AS
            SELECT bce.id, bce.agent_id, bce.belief_id, bce.collapsed_state,
                   bce.collapse_type, bce.collapse_fidelity, bce.created_at
            FROM belief_collapse_events bce
            WHERE bce.created_at > datetime('now', '-7 days')
            ORDER BY bce.created_at DESC;

CREATE TABLE belief_collapse_events (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    belief_id TEXT NOT NULL REFERENCES agent_beliefs(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    collapsed_state TEXT NOT NULL,
    measured_amplitude REAL NOT NULL,
    -- Expanded trigger type vocabulary (internal-ref)
    collapse_type TEXT NOT NULL,
    collapse_context TEXT DEFAULT NULL,
    collapse_fidelity REAL DEFAULT 1.0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_bce_belief ON belief_collapse_events(belief_id);

CREATE INDEX idx_bce_agent ON belief_collapse_events(agent_id);

CREATE INDEX idx_bce_type ON belief_collapse_events(collapse_type);

CREATE INDEX idx_bce_created ON belief_collapse_events(created_at DESC);

CREATE INDEX idx_access_log_task_id ON access_log(task_id) WHERE task_id IS NOT NULL;

CREATE TABLE memory_outcome_calibration (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id                TEXT NOT NULL,
    period_start            TEXT NOT NULL,
    period_end              TEXT NOT NULL,
    total_tasks             INTEGER NOT NULL DEFAULT 0,
    tasks_used_memory       INTEGER NOT NULL DEFAULT 0,
    success_with_memory     REAL,
    success_without_memory  REAL,
    brier_score             REAL,
    p_at_5                  REAL,
    computed_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_moc_agent_period ON memory_outcome_calibration(agent_id, period_start);

CREATE TABLE memory_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    trigger_condition TEXT NOT NULL,
    trigger_keywords TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_id INTEGER REFERENCES entities(id),
    memory_id INTEGER REFERENCES memories(id),
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','fired','expired','cancelled')),
    fired_at TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_triggers_status ON memory_triggers(status);

CREATE INDEX idx_triggers_agent ON memory_triggers(agent_id);

CREATE TABLE affect_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    valence REAL NOT NULL DEFAULT 0.0,
    arousal REAL NOT NULL DEFAULT 0.0,
    dominance REAL NOT NULL DEFAULT 0.0,
    affect_label TEXT,
    cluster TEXT,
    functional_state TEXT,
    safety_flag TEXT,
    trigger TEXT,
    source TEXT DEFAULT 'observation',
    metadata TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_affect_agent_time ON affect_log(agent_id, created_at DESC);

CREATE INDEX idx_affect_safety ON affect_log(safety_flag) WHERE safety_flag IS NOT NULL;

CREATE INDEX idx_affect_cluster ON affect_log(cluster, created_at DESC);

-- 2.2.3: cross-agent time-range index for `brainctl affect prune`. The
-- composite idx_affect_agent_time leads with agent_id and cannot serve a
-- WHERE created_at < ? predicate that spans all agents. Mirrors
-- migration 049_affect_log_retention_indexes.sql for fresh installs.
CREATE INDEX IF NOT EXISTS idx_affect_created_at ON affect_log(created_at);

-- -------------------------------------------------------------------------
-- LLM usage tracking
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    tool_name TEXT,          -- which MCP tool triggered the call (if applicable)
    project TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_agent_created ON llm_usage_log(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage_log(created_at);

-- Per-agent budget limits
CREATE TABLE IF NOT EXISTS agent_budget (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id),
    monthly_limit_usd REAL NOT NULL DEFAULT 10.0,
    alert_threshold REAL NOT NULL DEFAULT 0.8,   -- fraction of limit that triggers alert
    hard_limit REAL NOT NULL DEFAULT 1.0,         -- fraction at which calls are blocked
    reset_day INTEGER NOT NULL DEFAULT 1,         -- day of month budgets reset
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- -------------------------------------------------------------------------
-- Neuroscience-inspired memory columns (replay priority + reconsolidation)
-- -------------------------------------------------------------------------
-- replay_priority: accumulated salience score; higher = earlier consolidation
-- ripple_tags: count of high-salience (SWR-like) retrieval events
-- labile_until: ISO datetime when reconsolidation window closes (NULL = stable)
-- labile_agent_id: agent that opened the lability window (agent-scoped)
-- retrieval_prediction_error: cosine distance at lability-opening retrieval
-- (Columns are defined in the base CREATE TABLE memories above.)
CREATE INDEX IF NOT EXISTS idx_memories_replay ON memories(replay_priority DESC) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_labile ON memories(labile_until) WHERE labile_until IS NOT NULL;


-- -------------------------------------------------------------------------
-- Memory immunity system (issue #24)
-- Quarantine table for adversarial/injected memory detection
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    source_trust REAL,
    contradiction_count INTEGER DEFAULT 0,
    quarantined_by TEXT NOT NULL DEFAULT 'system',
    reviewed_by TEXT DEFAULT NULL,
    reviewed_at TEXT DEFAULT NULL,
    verdict TEXT DEFAULT NULL CHECK(verdict IN ('safe','malicious','uncertain')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE INDEX IF NOT EXISTS idx_quarantine_memory_id ON memory_quarantine(memory_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_verdict ON memory_quarantine(verdict);
CREATE INDEX IF NOT EXISTS idx_quarantine_created ON memory_quarantine(created_at DESC);

-- -------------------------------------------------------------------------
-- Allostatic scheduling (issue #9)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS consolidation_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    predicted_demand_at TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0.0 AND confidence <= 1.0),
    signal_source TEXT NOT NULL,
    fulfilled_at TEXT DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE INDEX IF NOT EXISTS idx_forecasts_agent ON consolidation_forecasts(agent_id, predicted_demand_at);
CREATE INDEX IF NOT EXISTS idx_forecasts_memory ON consolidation_forecasts(memory_id);
CREATE INDEX IF NOT EXISTS idx_forecasts_fulfilled ON consolidation_forecasts(fulfilled_at);

-- -------------------------------------------------------------------------
-- D-MEM RPE routing (issue #31)
-- memory_stats: per-(agent, category, scope) recall rate for long-term utility
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    category TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    avg_recall_rate REAL NOT NULL DEFAULT 0.5,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(agent_id, category, scope)
);
CREATE INDEX IF NOT EXISTS idx_memory_stats_agent ON memory_stats(agent_id, category, scope);

-- -------------------------------------------------------------------------
-- Temporal abstraction hierarchy (issue #20)
-- (temporal_level column is defined in the base CREATE TABLE memories above.)
-- -------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_memories_temporal_level ON memories(temporal_level, agent_id);

-- -------------------------------------------------------------------------
-- Context profiles — task-scoped search presets (brainctl profile)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_profiles (
    name         TEXT PRIMARY KEY,
    description  TEXT,
    categories   TEXT,
    tables       TEXT,
    entity_types TEXT,
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

-- ===========================================================================
-- FK INTEGRITY DELETE TRIGGERS  (mirrored from migration 048)
-- ===========================================================================
-- See db/migrations/048_fk_integrity_fts_retire_trigger.sql for full rationale.
-- These triggers fire only when PRAGMA foreign_keys = OFF (raw SQL admin,
-- merge.py:586 which disables FK during merge). With FK ON the SQLite default
-- NO ACTION rejects orphan-creating parent DELETEs outright.

CREATE TRIGGER IF NOT EXISTS trg_agent_delete_nullify_validation
AFTER DELETE ON agents
BEGIN
    UPDATE memories
       SET validation_agent_id = NULL
     WHERE validation_agent_id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_delete_cascade_edges
AFTER DELETE ON memories
BEGIN
    DELETE FROM knowledge_edges
     WHERE (source_table = 'memories' AND source_id = OLD.id)
        OR (target_table = 'memories' AND target_id = OLD.id);
END;

CREATE TRIGGER IF NOT EXISTS trg_entity_delete_cascade_edges
AFTER DELETE ON entities
BEGIN
    DELETE FROM knowledge_edges
     WHERE (source_table = 'entities' AND source_id = OLD.id)
        OR (target_table = 'entities' AND target_id = OLD.id);
END;

CREATE TRIGGER IF NOT EXISTS trg_event_delete_cascade_edges
AFTER DELETE ON events
BEGIN
    DELETE FROM knowledge_edges
     WHERE (source_table = 'events' AND source_id = OLD.id)
        OR (target_table = 'events' AND target_id = OLD.id);
END;

-- FTS5 retire-aware re-index: handled inline by the
-- memories_fts_update_insert trigger above, which has a `WHEN ... AND
-- new.retired_at IS NULL` guard. memories_fts_update_delete fires
-- unconditionally on any UPDATE when old.indexed = 1, which removes the
-- FTS5 row at the retire transition; the guarded _update_insert then does
-- NOT re-insert. Net: retired memories vanish from FTS5 immediately, no
-- separate purge trigger needed (and no double-delete risk).

-- Migration 051: code_ingest_cache — SHA256 cache for `brainctl ingest code`
-- (brainctl[code] optional extra, 2.4.4+). Included here so fresh installs
-- match upgrade-path schemas (caught by tests/test_schema_parity.py).
CREATE TABLE IF NOT EXISTS code_ingest_cache (
    file_path         TEXT NOT NULL,
    scope             TEXT NOT NULL DEFAULT 'global',
    content_sha       TEXT NOT NULL,
    language          TEXT NOT NULL,
    entity_count      INTEGER NOT NULL DEFAULT 0,
    edge_count        INTEGER NOT NULL DEFAULT 0,
    last_ingested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (file_path, scope)
);
CREATE INDEX IF NOT EXISTS idx_code_ingest_cache_scope
    ON code_ingest_cache(scope);
CREATE INDEX IF NOT EXISTS idx_code_ingest_cache_language
    ON code_ingest_cache(language);

-- ============================================================
-- Migrations 050, 053–057: thalamus + basal ganglia + cerebellum subsystems
-- (2026-05-15 evening cookoff). Mirrored here so fresh installs
-- match upgrade-path schemas — see tests/test_schema_parity.py.
-- ============================================================

-- ---- 050_thalamus.sql ----
-- Migration 050: thalamus Phase 1 schema
--
-- Adds an additive thalamus subsystem skeleton: typed relay catalog,
-- TRN-style gate sidecar, global mode row, salience cache, and sparse burst
-- log. Phase 1 is inspection-only: no existing tool behavior changes.
--
-- Rollback, if needed before live adoption:
--   DROP TABLE IF EXISTS thalamic_bursts;
--   DROP TABLE IF EXISTS thalamic_salience;
--   DROP TABLE IF EXISTS thalamic_mode;
--   DROP TABLE IF EXISTS thalamic_gate;
--   DROP TABLE IF EXISTS thalamic_relays;
--   DELETE FROM schema_version WHERE version = 50;
--   DELETE FROM schema_versions WHERE version = 50;
--
-- IDEMPOTENT: IF NOT EXISTS guards object creation; the seed rows use
-- INSERT OR IGNORE so repeated application does not duplicate state.

CREATE TABLE IF NOT EXISTS thalamic_relays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL UNIQUE,
    sector TEXT NOT NULL,
    driver_source TEXT NOT NULL,
    modulator_sources_json TEXT,
    target TEXT NOT NULL,
    transport TEXT NOT NULL CHECK(transport IN ('first_order', 'higher_order')),
    default_mode TEXT NOT NULL DEFAULT 'tonic' CHECK(default_mode IN ('tonic', 'burst')),
    default_gain REAL NOT NULL DEFAULT 1.0,
    topographic_key TEXT,
    efference_copy_target TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_relays_sector
    ON thalamic_relays(sector);

CREATE INDEX IF NOT EXISTS idx_relays_target
    ON thalamic_relays(target);

CREATE INDEX IF NOT EXISTS idx_relays_transport
    ON thalamic_relays(transport);

CREATE TABLE IF NOT EXISTS thalamic_gate (
    channel_id TEXT PRIMARY KEY,
    suppression REAL NOT NULL DEFAULT 0.0 CHECK(suppression >= 0.0 AND suppression <= 1.0),
    topdown_bias REAL NOT NULL DEFAULT 0.0 CHECK(topdown_bias >= 0.0 AND topdown_bias <= 1.0),
    bottomup_drive REAL NOT NULL DEFAULT 0.0 CHECK(bottomup_drive >= 0.0 AND bottomup_drive <= 1.0),
    sector TEXT NOT NULL,
    armed_for_burst INTEGER NOT NULL DEFAULT 0 CHECK(armed_for_burst IN (0, 1)),
    last_burst_at TEXT,
    bias_source TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    FOREIGN KEY (channel_id) REFERENCES thalamic_relays(channel_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gate_sector
    ON thalamic_gate(sector);

CREATE INDEX IF NOT EXISTS idx_gate_armed
    ON thalamic_gate(armed_for_burst)
    WHERE armed_for_burst = 1;

CREATE TABLE IF NOT EXISTS thalamic_mode (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    mode TEXT NOT NULL DEFAULT 'wake_focused'
        CHECK(mode IN ('wake_focused', 'wake_exploratory', 'drowsy', 'consolidate', 'offline')),
    arousal REAL NOT NULL DEFAULT 0.5 CHECK(arousal >= 0.0 AND arousal <= 1.0),
    acetylcholine REAL NOT NULL DEFAULT 0.5 CHECK(acetylcholine >= 0.0 AND acetylcholine <= 1.0),
    norepinephrine REAL NOT NULL DEFAULT 0.5 CHECK(norepinephrine >= 0.0 AND norepinephrine <= 1.0),
    retrieval_breadth_multiplier REAL NOT NULL DEFAULT 1.0,
    similarity_threshold_delta REAL NOT NULL DEFAULT 0.0,
    set_by TEXT,
    set_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

INSERT OR IGNORE INTO thalamic_mode (id, mode)
VALUES (1, 'wake_focused');

CREATE TABLE IF NOT EXISTS thalamic_salience (
    candidate_id TEXT NOT NULL,
    candidate_type TEXT NOT NULL CHECK(candidate_type IN ('memory', 'event', 'belief', 'entity', 'relay')),
    bottomup_score REAL,
    topdown_score REAL,
    precision REAL NOT NULL DEFAULT 1.0,
    integrated REAL,
    sector TEXT,
    computed_for_agent TEXT NOT NULL,
    computed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (candidate_id, candidate_type, computed_for_agent)
);

CREATE INDEX IF NOT EXISTS idx_salience_recent
    ON thalamic_salience(computed_at);

CREATE INDEX IF NOT EXISTS idx_salience_integrated
    ON thalamic_salience(integrated DESC);

CREATE INDEX IF NOT EXISTS idx_salience_agent
    ON thalamic_salience(computed_for_agent, integrated DESC);

CREATE TABLE IF NOT EXISTS thalamic_bursts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    sector TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_ref TEXT,
    salience REAL NOT NULL,
    fired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    consumed_by TEXT,
    consumed_at TEXT,
    FOREIGN KEY (channel_id) REFERENCES thalamic_relays(channel_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bursts_unconsumed
    ON thalamic_bursts(consumed_at)
    WHERE consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_bursts_channel_time
    ON thalamic_bursts(channel_id, fired_at DESC);

CREATE INDEX IF NOT EXISTS idx_bursts_sector_time
    ON thalamic_bursts(sector, fired_at DESC);

-- ---- 053_thalamus_shadow.sql ----
-- Migration 053: thalamus Phase 2 shadow-mode decision log
--
-- Phase 2 of the thalamus subsystem (per docs/proposals/thalamus.md) adds
-- writeable gate / burst / mode tools and a shadow consult at the W(m) write
-- gate. The hookpoint never alters production behavior; it records what the
-- thalamic gate WOULD have done so we can compare against actual outcomes
-- before flipping to enforcement mode in a future phase.
--
-- This migration adds the append-only audit table that the shadow consult
-- writes to.
--
-- Rollback, if needed before live adoption:
--   DROP TABLE IF EXISTS thalamic_shadow_decisions;
--   DELETE FROM schema_version WHERE version = 53;
--
-- IDEMPOTENT: IF NOT EXISTS guards object creation.

CREATE TABLE IF NOT EXISTS thalamic_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    source_call TEXT NOT NULL,
    sector TEXT,
    channel_id TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    suppression REAL,
    bottomup_drive REAL,
    surprise_score REAL,
    actual_outcome TEXT,
    payload_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_shadow_recent
    ON thalamic_shadow_decisions(decision_at);

CREATE INDEX IF NOT EXISTS idx_shadow_sector_recent
    ON thalamic_shadow_decisions(sector, decision_at);

CREATE INDEX IF NOT EXISTS idx_shadow_decision_recent
    ON thalamic_shadow_decisions(decision, decision_at);

-- ---- 054_basal_ganglia.sql ----
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

-- ---- 055_basal_ganglia_shadow.sql ----
-- Migration 055: basal ganglia Phase 2 — shadow-mode dispatch decision log
--
-- Phase 2 of the BG subsystem wires the TD-error broadcast bus into
-- outcome_annotate and adds a shadow consult at the tool-dispatch entry
-- point (mcp_server.py:3247). The shadow consult never alters dispatch
-- behavior; it records what the BG would have decided (approve / block /
-- delay / delegate) so we can validate the policy against actual outcomes
-- before flipping to enforcement mode.
--
-- Rollback, if needed:
--   DROP TABLE IF EXISTS bg_shadow_decisions;
--   DELETE FROM schema_version WHERE version = 55;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS bg_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id TEXT,
    action_key TEXT NOT NULL,
    loop TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    net_signal REAL,
    w_go REAL,
    w_nogo REAL,
    context_hash TEXT,
    arguments_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_bg_shadow_recent
    ON bg_shadow_decisions(decision_at);

CREATE INDEX IF NOT EXISTS idx_bg_shadow_decision
    ON bg_shadow_decisions(decision, decision_at);

CREATE INDEX IF NOT EXISTS idx_bg_shadow_action
    ON bg_shadow_decisions(action_key, decision_at);

-- ---- 056_cerebellum.sql ----
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

-- ---- 058_amygdala.sql ----
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

-- ---- 059_hippocampal_subfields.sql ----
-- Migration 059: hippocampal subfields — DG / CA3 / CA1 split
--
-- brainctl's existing hippocampus.py treats the hippocampus as a flat
-- abstraction (one consolidation pipeline). Biology splits it into:
--   DG  (dentate gyrus)  — PATTERN SEPARATION: orthogonalizes inputs
--                          so similar contexts get distinct neural codes
--                          before they hit the storage substrate.
--   CA3 (CA3 region)     — PATTERN COMPLETION: given a partial cue,
--                          completes via recurrent collaterals to the
--                          full stored pattern.
--   CA1 (CA1 region)     — OUTPUT + comparison: routes the completed
--                          pattern to entorhinal cortex and on to cortex.
--
-- brainctl today is implicitly CA3 (similarity-based recall) without an
-- explicit DG step at write time. That means near-duplicate memories
-- pattern-complete onto each other rather than being stored as distinct
-- traces — a known source of retrieval interference under W(m).
--
-- This phase adds:
--   hippocampus_pattern_separations  — DG audit table: every write that
--                                       had a near-neighbor in scope gets
--                                       a row recording the separation
--                                       (distance, decision, assigned tag).
--   hippocampus_completion_traces    — CA3 audit table: every retrieval
--                                       that pattern-completed past a
--                                       similarity threshold gets a row
--                                       (cue, completed_to, distance).
--
-- These are AUDIT/LEARNING tables, not new storage. The actual memories
-- live in the existing `memories` table; subfields just annotate.
--
-- Rollback:
--   DROP TABLE IF EXISTS hippocampus_completion_traces;
--   DROP TABLE IF EXISTS hippocampus_pattern_separations;
--   DELETE FROM schema_version WHERE version = 59;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS hippocampus_pattern_separations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER,                    -- new memory being written
    nearest_neighbor_id INTEGER,          -- closest existing memory in scope
    cosine_distance REAL NOT NULL,        -- 0=identical, 1=orthogonal
    decision TEXT NOT NULL CHECK(decision IN ('separate','merge','deduplicate','passthrough')),
    separation_tag TEXT,                  -- when 'separate', tag distinguishing
                                          -- the new code from the neighbor
    scope TEXT,
    agent_id TEXT,
    decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_hs_separations_memory ON hippocampus_pattern_separations(memory_id);
CREATE INDEX IF NOT EXISTS idx_hs_separations_recent ON hippocampus_pattern_separations(decided_at);

CREATE TABLE IF NOT EXISTS hippocampus_completion_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_hash TEXT NOT NULL,             -- hash of the original cue
    completed_to_memory_id INTEGER NOT NULL,
    distance REAL NOT NULL,               -- how far the cue was from the
                                          -- completed pattern
    rank INTEGER NOT NULL DEFAULT 1,      -- 1 = top match, etc.
    agent_id TEXT,
    completed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_hs_completion_recent ON hippocampus_completion_traces(completed_at);
CREATE INDEX IF NOT EXISTS idx_hs_completion_memory ON hippocampus_completion_traces(completed_to_memory_id);

-- ---- 060_acc.sql ----
-- Migration 060: ACC — in-flight conflict / error monitor
-- Watches LIVE operations (memory_add, belief_set, entity_observe, workspace_broadcast)
-- and emits a scalar control-demand signal. Distinct from reflexion (after-fact lessons)
-- and from belief_conflicts (static contradictions in the DB).
-- Phase 1 is audit-only.
CREATE TABLE IF NOT EXISTS acc_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    agent_id TEXT,
    op_kind TEXT NOT NULL,
    op_scope TEXT,
    conflict_score REAL NOT NULL DEFAULT 0.0,
    surprise_score REAL NOT NULL DEFAULT 0.0,
    evc_score REAL NOT NULL DEFAULT 0.0,
    action TEXT NOT NULL DEFAULT 'log' CHECK(action IN ('log','warn','hold_fired','ignore')),
    fired_hold_id INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_acc_events_recent ON acc_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_acc_events_scope ON acc_events(op_scope, occurred_at DESC);

-- 5-second co-activation window: in-flight operations registered before commit.
CREATE TABLE IF NOT EXISTS acc_inflight (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    expires_at TEXT NOT NULL,
    agent_id TEXT,
    op_kind TEXT NOT NULL,
    op_scope TEXT NOT NULL,
    op_hash TEXT,
    intent_payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_acc_inflight_scope ON acc_inflight(op_scope, expires_at);

-- Learned outcome predictions per (op_kind, op_scope) — RVPM-style.
CREATE TABLE IF NOT EXISTS acc_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    op_kind TEXT NOT NULL,
    op_scope TEXT NOT NULL,
    n_trials INTEGER NOT NULL DEFAULT 0,
    n_conflicts INTEGER NOT NULL DEFAULT 0,
    p_conflict REAL NOT NULL DEFAULT 0.5,
    volatility REAL NOT NULL DEFAULT 0.5,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(op_kind, op_scope)
);

-- ---- 061_dmn.sql ----
-- Migration 061: DMN — offline simulation / counterfactual rollouts
-- Schacter's "constructive episodic simulation" — recombine memories+entities
-- into plausible futures. Speculative memories live in a QUARANTINED table
-- (no FTS5, no vector index) so they never poison default retrieval.

CREATE TABLE IF NOT EXISTS dmn_simulations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    seed_type TEXT NOT NULL CHECK(seed_type IN ('entity','memory','event')),
    seed_id INTEGER NOT NULL,
    scope TEXT,
    scenario TEXT NOT NULL,
    plausibility REAL NOT NULL DEFAULT 0.5,
    novelty REAL NOT NULL DEFAULT 0.5,
    utility REAL NOT NULL DEFAULT 0.5,
    composite_score REAL NOT NULL DEFAULT 0.5,
    triggered_by TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    retired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dmn_sims_agent ON dmn_simulations(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dmn_sims_score ON dmn_simulations(composite_score DESC);

-- QUARANTINED — no FTS5/vec triggers, no default retrieval visibility.
CREATE TABLE IF NOT EXISTS dmn_speculative_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id INTEGER NOT NULL REFERENCES dmn_simulations(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT,
    scope TEXT,
    confidence REAL NOT NULL DEFAULT 0.3,
    validation_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(validation_state IN ('pending','corroborated','falsified','expired')),
    validated_against_event_id INTEGER,
    promoted_memory_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dmn_spec_state ON dmn_speculative_memories(validation_state, expires_at);

CREATE TABLE IF NOT EXISTS dmn_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    next_run_at TEXT,
    last_run_at TEXT,
    idle_threshold_s INTEGER NOT NULL DEFAULT 600,
    new_mem_threshold INTEGER NOT NULL DEFAULT 100,
    max_sims_per_run INTEGER NOT NULL DEFAULT 5,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(agent_id)
);

-- ---- 062_drives.sql ----
-- Migration 062: Drives / hypothalamus — homeostatic set-points
-- Named needs (consolidation_debt, staleness, belief_coverage, pii_pressure,
-- entity_freshness) with set-points; current levels produce drive magnitudes
-- that BIAS — but never directly override — downstream gating.

CREATE TABLE IF NOT EXISTS drive_definitions (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    set_point REAL NOT NULL,
    hard_threshold REAL,
    sample_query TEXT NOT NULL,
    recommended_mode TEXT,
    is_safety_drive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS drive_current_state (
    name TEXT PRIMARY KEY REFERENCES drive_definitions(name) ON DELETE CASCADE,
    current_level REAL NOT NULL DEFAULT 0.0,
    error REAL NOT NULL DEFAULT 0.0,
    magnitude REAL NOT NULL DEFAULT 0.0,
    in_hard_state INTEGER NOT NULL DEFAULT 0,
    sampled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS drive_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    current_level REAL NOT NULL,
    error REAL NOT NULL,
    magnitude REAL NOT NULL,
    sampled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
CREATE INDEX IF NOT EXISTS idx_drive_history_name_time ON drive_history(name, sampled_at DESC);

-- Seed the five canonical brainctl drives.
INSERT OR IGNORE INTO drive_definitions (name, description, set_point, hard_threshold, sample_query, recommended_mode, is_safety_drive) VALUES
  ('consolidation_debt', 'Memories with low replay_priority and stale last_recalled_at', 0.0, NULL, 'SELECT 0.0', 'incident', 0),
  ('staleness',          'Time since last event in active project scope (hours)', 6.0, NULL, 'SELECT 0.0', 'wake_exploratory', 0),
  ('belief_coverage',    'Fraction of entities with NULL compiled_truth', 0.7, NULL, 'SELECT 0.0', NULL, 0),
  ('pii_pressure',       'Unprocessed PII recency queue depth', 0.0, 10.0, 'SELECT 0.0', NULL, 1),
  ('entity_freshness',   'Fraction of entities without recent entity_observe (14d)', 0.6, NULL, 'SELECT 0.0', 'focused_work', 0);

-- ---- 063_insula.sql ----
-- Migration 063: Insula — interoception / self-state subsystem
-- Maps brainctl's internal state (queue depths, error rates, retrieval
-- latency, write pressure, certainty) into a unified felt-state vector
-- that other subsystems subscribe to. Predicted baseline + deviation —
-- consumers react to PREDICTION ERROR on interoceptive signals.

CREATE TABLE IF NOT EXISTS insula_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_name TEXT NOT NULL,
    raw_value REAL NOT NULL,
    normalized_value REAL NOT NULL CHECK(normalized_value >= 0.0 AND normalized_value <= 1.0),
    baseline_ema REAL,
    deviation REAL,
    source TEXT,
    sampled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
CREATE INDEX IF NOT EXISTS idx_insula_signals_name_time ON insula_signals(signal_name, sampled_at DESC);

-- Singleton aggregate — the current felt state.
CREATE TABLE IF NOT EXISTS insula_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    write_pressure REAL NOT NULL DEFAULT 0.0,
    retrieval_strain REAL NOT NULL DEFAULT 0.0,
    consolidation_debt REAL NOT NULL DEFAULT 0.0,
    embedding_health REAL NOT NULL DEFAULT 1.0,
    attention_load REAL NOT NULL DEFAULT 0.0,
    certainty REAL NOT NULL DEFAULT 0.5,
    felt_state_label TEXT NOT NULL DEFAULT 'calm',
    urgency_score REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
INSERT OR IGNORE INTO insula_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS insula_subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subsystem TEXT NOT NULL,
    signal_name TEXT NOT NULL,
    threshold REAL NOT NULL,
    comparator TEXT NOT NULL DEFAULT 'gt' CHECK(comparator IN ('gt','lt','abs_gt')),
    action_hint TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired_at TEXT,
    UNIQUE(subsystem, signal_name, action_hint)
);

-- ---- 064_pfc_slots.sql ----
-- Migration 064: PFC sub-regions — named slots
-- dlPFC = active task / WM, vmPFC = outcome-utility, OFC = realized-outcome,
-- frontopolar = meta-monitor. The substrate already exists scattered across
-- mcp_tools_consolidation/trust/reflexion/agents. This subsystem is mostly
-- AGGREGATION + ROUTING — one small table for named-slot state that agents
-- can fill and reread.

CREATE TABLE IF NOT EXISTS pfc_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    slot TEXT NOT NULL CHECK(slot IN ('dlpfc','vmpfc','ofc','frontopolar')),
    content TEXT NOT NULL,         -- JSON payload
    confidence REAL NOT NULL DEFAULT 0.5,
    last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(agent_id, slot)
);
CREATE INDEX IF NOT EXISTS idx_pfc_slots_agent ON pfc_slots(agent_id);

-- ---- 065_entorhinal_grid.sql ----
-- Migration 065: Entorhinal cortex — conceptual grid indexing
-- brainctl has temporal grid (epochs) but no conceptual grid. Grid cells in
-- EC tile concept-space with periodic basis functions; each memory activates
-- a small subset of cells, giving cheap pattern-indexed lookup.
-- Phase 1: schema + audit. Phase 2: wire into retrieval.

CREATE TABLE IF NOT EXISTS entorhinal_grid_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scale INTEGER NOT NULL,         -- grid scale (1=fine, 2=medium, 3=coarse...)
    cell_index INTEGER NOT NULL,    -- index within the scale
    basis_hash TEXT NOT NULL,       -- hash of the periodic basis function used
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(scale, cell_index)
);

-- Per-memory grid activations: which cells fire for this memory.
CREATE TABLE IF NOT EXISTS entorhinal_memory_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL,
    cell_id INTEGER NOT NULL REFERENCES entorhinal_grid_cells(id) ON DELETE CASCADE,
    activation REAL NOT NULL DEFAULT 1.0,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(memory_id, cell_id)
);
CREATE INDEX IF NOT EXISTS idx_eg_activations_memory ON entorhinal_memory_activations(memory_id);
CREATE INDEX IF NOT EXISTS idx_eg_activations_cell ON entorhinal_memory_activations(cell_id, activation DESC);

-- Seed canonical 3-scale grid cells (16 cells per scale = 48 total).
INSERT OR IGNORE INTO entorhinal_grid_cells (scale, cell_index, basis_hash, description)
SELECT 1, n, 'fine:' || n, 'fine-grained grid cell ' || n FROM (
    SELECT 0 AS n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3
    UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7
    UNION SELECT 8 UNION SELECT 9 UNION SELECT 10 UNION SELECT 11
    UNION SELECT 12 UNION SELECT 13 UNION SELECT 14 UNION SELECT 15);
INSERT OR IGNORE INTO entorhinal_grid_cells (scale, cell_index, basis_hash, description)
SELECT 2, n, 'medium:' || n, 'medium-grained grid cell ' || n FROM (
    SELECT 0 AS n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3
    UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7
    UNION SELECT 8 UNION SELECT 9 UNION SELECT 10 UNION SELECT 11
    UNION SELECT 12 UNION SELECT 13 UNION SELECT 14 UNION SELECT 15);
INSERT OR IGNORE INTO entorhinal_grid_cells (scale, cell_index, basis_hash, description)
SELECT 3, n, 'coarse:' || n, 'coarse-grained grid cell ' || n FROM (
    SELECT 0 AS n UNION SELECT 1 UNION SELECT 2 UNION SELECT 3
    UNION SELECT 4 UNION SELECT 5 UNION SELECT 6 UNION SELECT 7
    UNION SELECT 8 UNION SELECT 9 UNION SELECT 10 UNION SELECT 11
    UNION SELECT 12 UNION SELECT 13 UNION SELECT 14 UNION SELECT 15);
