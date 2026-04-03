-- =============================================================================
-- Agent Memory Spine — Unified memory database for all agents
-- Location: ~/agentmemory/db/brain.db
-- Engine: SQLite 3.51+ with WAL mode, FTS5
-- =============================================================================

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA cache_size = -64000;  -- 64MB cache
PRAGMA mmap_size = 268435456; -- 256MB mmap for reads
PRAGMA temp_store = MEMORY;

-- =============================================================================
-- SCHEMA VERSION
-- =============================================================================

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    description TEXT
);

INSERT INTO schema_version (version, description) VALUES (1, 'Initial schema — unified agent memory spine');

-- =============================================================================
-- AGENTS — registered agents that can read/write
-- =============================================================================

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,                      -- e.g. 'hermes', 'agent-1', 'hippocampus'
    display_name TEXT NOT NULL,
    agent_type TEXT NOT NULL,                 -- 'hermes', 'agent', 'human', etc.
    adapter_info TEXT,                        -- JSON: connection details, model, etc
    status TEXT NOT NULL DEFAULT 'active',    -- active, paused, retired
    last_seen_at TEXT,
    attention_class TEXT NOT NULL DEFAULT 'ic',       -- 'exec' | 'ic' | 'peripheral' | 'dormant'
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

-- =============================================================================
-- MEMORIES — durable facts (replaces memory.md, user.md, shared-brain curated files)
-- =============================================================================

CREATE TABLE IF NOT EXISTS memories (
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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    retired_at TEXT                                    -- soft delete
);

CREATE INDEX idx_memories_agent ON memories(agent_id);
CREATE INDEX idx_memories_category ON memories(category);
CREATE INDEX idx_memories_scope ON memories(scope);
CREATE INDEX idx_memories_active ON memories(retired_at) WHERE retired_at IS NULL;
CREATE INDEX idx_memories_confidence ON memories(confidence DESC);

-- FTS5 for full-text search across memories
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    category,
    tags,
    content=memories,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- Keep FTS in sync
CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags) VALUES (new.id, new.content, new.category, new.tags);
END;

CREATE TRIGGER memories_fts_update AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags) VALUES('delete', old.id, old.content, old.category, old.tags);
    INSERT INTO memories_fts(rowid, content, category, tags) VALUES (new.id, new.content, new.category, new.tags);
END;

CREATE TRIGGER memories_fts_delete AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags) VALUES('delete', old.id, old.content, old.category, old.tags);
END;

-- =============================================================================
-- EVENTS — structured log of everything that happened (replaces 10_EVENTS.jsonl)
-- =============================================================================

CREATE TABLE IF NOT EXISTS events (
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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX idx_events_agent ON events(agent_id);
CREATE INDEX idx_events_type ON events(event_type);
CREATE INDEX idx_events_project ON events(project);
CREATE INDEX idx_events_session ON events(session_id);
CREATE INDEX idx_events_time ON events(created_at DESC);
CREATE INDEX idx_events_importance ON events(importance DESC);

-- FTS5 for event search
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
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

-- =============================================================================
-- CONTEXT — chunked, searchable knowledge from documents, conversations, code
-- =============================================================================

CREATE TABLE IF NOT EXISTS context (
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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    stale_at TEXT                                    -- when source was re-indexed
);

CREATE INDEX idx_context_source ON context(source_type, source_ref);
CREATE INDEX idx_context_project ON context(project);
CREATE INDEX idx_context_stale ON context(stale_at) WHERE stale_at IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS context_fts USING fts5(
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

-- =============================================================================
-- TASKS — shared task state across agents (replaces 03_TASKS.md)
-- =============================================================================

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT,                              -- External issue ID, GitHub issue #, etc
    external_system TEXT,                           -- 'github', 'jira', 'manual', etc.
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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_agent ON tasks(assigned_agent_id);
CREATE INDEX idx_tasks_project ON tasks(project);
CREATE INDEX idx_tasks_external ON tasks(external_system, external_id);

-- =============================================================================
-- DECISIONS — durable decisions log (replaces 02_DECISIONS.md)
-- =============================================================================

CREATE TABLE IF NOT EXISTS decisions (
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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX idx_decisions_project ON decisions(project);
CREATE INDEX idx_decisions_agent ON decisions(agent_id);

-- =============================================================================
-- EMBEDDINGS — Phase 2 vector store (schema ready, populated later)
-- =============================================================================

CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,                     -- 'memories', 'context', 'events'
    source_id INTEGER NOT NULL,
    model TEXT NOT NULL,                            -- embedding model used
    dimensions INTEGER NOT NULL,
    vector BLOB,                                    -- raw float32 vector (or use sqlite-vec later)
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX idx_embeddings_source ON embeddings(source_table, source_id);

-- =============================================================================
-- AGENT_STATE — per-agent checkpoints and runtime state
-- =============================================================================

CREATE TABLE IF NOT EXISTS agent_state (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,                            -- JSON value
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (agent_id, key)
);

-- =============================================================================
-- BLOBS — large artifacts (files, screenshots, exports)
-- Reference: ~/agentmemory/blobs/<sha256>.<ext>
-- =============================================================================

CREATE TABLE IF NOT EXISTS blobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL,
    disk_path TEXT NOT NULL,                        -- relative path under ~/agentmemory/blobs/
    agent_id TEXT REFERENCES agents(id),
    project TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX idx_blobs_sha256 ON blobs(sha256);
CREATE INDEX idx_blobs_project ON blobs(project);

-- =============================================================================
-- ACCESS LOG — who read/wrote what (audit trail)
-- =============================================================================

CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,                           -- 'read', 'write', 'search', 'promote', 'retire'
    target_table TEXT,
    target_id INTEGER,
    query TEXT,                                      -- search query if action=search
    result_count INTEGER,
    tokens_consumed INTEGER,                         -- token cost of the operation
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX idx_access_agent ON access_log(agent_id);
CREATE INDEX idx_access_time ON access_log(created_at DESC);

-- Prune access log older than 30 days automatically
-- (run via brainctl maintenance command)
