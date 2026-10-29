-- 026_outcome_calibration.sql
-- : Outcome-Linked Memory Evaluation
-- Annotate access_log with task outcome signals; add calibration table.

-- access_log outcome annotation columns
ALTER TABLE access_log ADD COLUMN task_id TEXT;
ALTER TABLE access_log ADD COLUMN task_outcome TEXT
    CHECK (task_outcome IN ('success', 'blocked', 'escalated', 'cancelled'));
ALTER TABLE access_log ADD COLUMN pre_task_uncertainty REAL;
ALTER TABLE access_log ADD COLUMN retrieval_contributed INTEGER DEFAULT NULL
    CHECK (retrieval_contributed IN (0, 1, NULL));

-- Calibration table: stores periodic memory lift + Brier score snapshots
CREATE TABLE IF NOT EXISTS memory_outcome_calibration (
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

CREATE INDEX IF NOT EXISTS idx_access_log_task_id ON access_log(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_moc_agent_period ON memory_outcome_calibration(agent_id, period_start);
