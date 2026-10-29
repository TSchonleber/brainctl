-- Migration 046: Add search instrumentation columns to agent_uncertainty_log
-- (RENUMBERED from original 023 in v2.2.0 — see migrate.py header
-- comment "DUPLICATE-VERSION HISTORY" for context.)
-- Author: Engram (Memory Systems Lead)
-- Date: 2026-03-28 (original); renumbered 2026-04-16
-- Purpose: Extend agent_uncertainty_log with passive search telemetry columns so that
--          brainctl search can append a row capturing per-query metrics.
--          These columns coexist with the pre-task gap-tracking columns added in migration
--          022. Gap-tracking rows fill gap_topic/free_energy; search rows fill
--          domain/query/result_count/avg_confidence.
--
-- IDEMPOTENT:
--  * `ADD COLUMN` rows are guarded at runtime by migrate.py
--    (`_apply_sql` strips out duplicate-column failures so re-applying
--    is safe). SQLite has no native `ADD COLUMN IF NOT EXISTS` syntax.
--  * Indexes use IF NOT EXISTS.
--
-- HISTORICAL NOTE: the original file (023_uncertainty_log_search_columns.sql)
-- ended with an "ADD COLUMN IF NOT EXISTS" statement against access_log.
-- tokens_consumed which is a SQLite syntax error. That line was a
-- duplicate of the column already added in 023_attention_budget.sql, and
-- is removed here. (The literal SQL is paraphrased above so the
-- status_verbose ADD COLUMN regex does not capture this comment.)

ALTER TABLE agent_uncertainty_log ADD COLUMN domain         TEXT;
ALTER TABLE agent_uncertainty_log ADD COLUMN query          TEXT;
ALTER TABLE agent_uncertainty_log ADD COLUMN result_count   INTEGER;
ALTER TABLE agent_uncertainty_log ADD COLUMN avg_confidence REAL;
ALTER TABLE agent_uncertainty_log ADD COLUMN retrieved_at   DATETIME DEFAULT (datetime('now'));
ALTER TABLE agent_uncertainty_log ADD COLUMN temporal_class TEXT     DEFAULT 'ephemeral';
ALTER TABLE agent_uncertainty_log ADD COLUMN ttl_days       INTEGER  DEFAULT 30;

CREATE INDEX IF NOT EXISTS idx_unc_domain     ON agent_uncertainty_log(domain);
CREATE INDEX IF NOT EXISTS idx_unc_retrieved  ON agent_uncertainty_log(retrieved_at);

INSERT INTO schema_version (version, applied_at, description)
VALUES (46, datetime('now'),
    'agent_uncertainty_log — search instrumentation columns: domain, query, result_count, avg_confidence, retrieved_at, temporal_class, ttl_days (renumbered from 023)');

PRAGMA user_version = 46;
