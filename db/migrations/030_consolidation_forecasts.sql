-- Migration 030: Allostatic scheduling — consolidation_forecasts table (issue #9)
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
