-- Migration 066: retrieval pathway log — issue #116 Phase 1-A
--
-- Sidecar observation log for memory_search dispatches. Records the
-- pathway fingerprint (mode, table_distribution, intent, profile,
-- candidate counts, latency) per retrieval. Independent of bg_td_events
-- by design — emission is always safe regardless of whether the BG
-- subsystem is active, and the absence of an outcome signal does not
-- prevent the observation from being recorded.
--
-- The memo (issue #116, §6.3 Step 1) argues for an observation surface
-- that separates "what happened during retrieval" from "what the outcome
-- was". bg_td_events covers the outcome side; this table covers the
-- retrieval side. Joins are agent_id + time-window based, deliberately
-- loose, so the two ledgers can evolve independently.
--
-- Granularity: one row per cmd_search invocation that reaches output
-- assembly. Emission is best-effort and gated behind
-- BRAINCTL_PATHWAY_LOG=0 (env-var kill switch).
--
-- Rollback, if needed:
--   DROP INDEX IF EXISTS idx_rpl_intent;
--   DROP INDEX IF EXISTS idx_rpl_mode;
--   DROP INDEX IF EXISTS idx_rpl_agent;
--   DROP INDEX IF EXISTS idx_rpl_recent;
--   DROP TABLE IF EXISTS retrieval_pathway_log;
--   DELETE FROM schema_version WHERE version = 66;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS retrieval_pathway_log (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at                 TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    agent_id                 TEXT,
    project                  TEXT,
    query                    TEXT,
    query_hash               TEXT,           -- BLAKE2b-128 hex of normalized query, for dedupe / cluster lookup
    -- Pathway fingerprint (what happened during retrieval):
    mode                     TEXT,           -- 'fts' | 'hybrid-rrf' | 'vector' (values from cmd_search's `mode` var; not CHECK-constrained so callers can extend)
    table_distribution       TEXT,           -- json: {"memories": 7, "events": 2, "decisions": 1}
    tables_searched          TEXT,           -- json list of tables actually searched (post intent-router / profile / query-planner)
    candidate_count_pre      INTEGER,        -- pre-rerank candidate count when computable; NULL otherwise
    candidate_count_post     INTEGER,        -- final result count returned
    rrf_contribution_ratio   REAL,           -- fraction of top-K primarily from FTS rank in hybrid mode (NULL when not computed; not surfaced yet, reserved for Phase 1-B+ wiring)
    -- Motivational context (will be populated by Phase 1-B motivational entry gate):
    intent_label             TEXT,           -- from intent_classifier (e.g. 'entity_lookup', 'event_lookup', 'general')
    active_profile           TEXT,           -- 'ops' | 'research' | 'writing' | 'meeting' | 'networking' | NULL
    suppressed_strategies    TEXT,           -- json list of strategies the motivational gate excluded; NULL when no gate fired
    -- Runtime metadata:
    embedding_model_version  TEXT,           -- e.g. 'nomic-embed-text:768' — added so hashes/clusters survive embedding-model rotation
    latency_ms               INTEGER,
    benchmark_mode           INTEGER NOT NULL DEFAULT 0,  -- 1 when --benchmark short-circuited the reranker chain
    -- Loose coupling to bg_td_events (no FK enforced — outcomes may arrive much later or not at all):
    linked_td_event_id       INTEGER         -- nullable; populated later by a separate linker if/when outcome is observed
);

CREATE INDEX IF NOT EXISTS idx_rpl_recent
    ON retrieval_pathway_log(fired_at);
CREATE INDEX IF NOT EXISTS idx_rpl_agent
    ON retrieval_pathway_log(agent_id, fired_at);
CREATE INDEX IF NOT EXISTS idx_rpl_mode
    ON retrieval_pathway_log(mode, fired_at);
CREATE INDEX IF NOT EXISTS idx_rpl_intent
    ON retrieval_pathway_log(intent_label, fired_at);
CREATE INDEX IF NOT EXISTS idx_rpl_unlinked
    ON retrieval_pathway_log(linked_td_event_id) WHERE linked_td_event_id IS NULL;

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (66, 'issue #116 Phase 1-A: retrieval pathway log',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
