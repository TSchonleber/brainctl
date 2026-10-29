-- Migration 016: Incubation Queue — Deferred Query Retry for Dream Pass
-- Author: Prune (Memory Hygiene Specialist)
-- Date: 2026-03-28
-- Purpose: Log zero-result brainctl search queries so the consolidation
--          dream pass can retry them with expanded thresholds and surface
--          matches as memory retrieval suggestions.
-- References: research/wave6/24_creative_synthesis_dreams.md , -- Schema version: 15 -> 16

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

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (16, datetime('now'),
  'deferred_queries table — incubation queue for zero-result search retry ');

PRAGMA user_version = 16;
