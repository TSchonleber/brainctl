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

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (64, 'PFC sub-regions: 4 named slots (dlpfc/vmpfc/ofc/frontopolar) per agent', strftime('%Y-%m-%dT%H:%M:%S','now'));
