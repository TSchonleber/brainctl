-- Migration 022: Active Inference Layer — agent_uncertainty_log -- Author: Cortex (Intelligence Synthesis Analyst)
-- Date: 2026-03-28
-- Purpose: Track per-task knowledge gaps and their resolution for the Active Inference Layer.
--          Enables empirical measurement of what agents don't know before tasks, and whether
--          gaps were filled. Feeds brainctl infer-pretask / infer-gapfill commands.
-- References: , (Wave 10 Active Inference research)
-- Schema version: 21 -> 22

CREATE TABLE IF NOT EXISTS agent_uncertainty_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    task_desc       TEXT,                                    -- task description that triggered the scan
    gap_topic       TEXT,                                    -- what the agent didn't know
    free_energy     REAL,                                    -- (1 - confidence) * importance at scan time
    resolved_at     TIMESTAMP,                               -- when the gap was filled
    resolved_by     INTEGER REFERENCES memories(id),         -- memory that resolved the gap
    propagated      BOOLEAN DEFAULT FALSE,                   -- whether gap was propagated to other agents
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_unc_agent     ON agent_uncertainty_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_unc_created   ON agent_uncertainty_log(created_at);
CREATE INDEX IF NOT EXISTS idx_unc_resolved  ON agent_uncertainty_log(resolved_at);
CREATE INDEX IF NOT EXISTS idx_unc_task      ON agent_uncertainty_log(agent_id, resolved_at);

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (22, datetime('now'),
    'agent_uncertainty_log — Active Inference Layer pre-task gap tracking ');

PRAGMA user_version = 22;
