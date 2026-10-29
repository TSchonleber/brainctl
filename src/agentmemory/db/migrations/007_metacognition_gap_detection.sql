-- Migration 007: Metacognition & Gap Detection
-- Implements the metacognitive layer from / research.
-- Adds knowledge_coverage and knowledge_gaps tables so agents can distinguish
-- "I know this" from "I have a blind spot here."

-- Coverage index: per-scope aggregates of what Hermes actually knows
CREATE TABLE IF NOT EXISTS knowledge_coverage (
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

CREATE INDEX IF NOT EXISTS idx_coverage_scope ON knowledge_coverage(scope);
CREATE INDEX IF NOT EXISTS idx_coverage_density ON knowledge_coverage(coverage_density DESC);

-- Gap registry: explicit blind spots detected by the gap scanner
CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_type TEXT NOT NULL CHECK(gap_type IN (
        'coverage_hole',      -- no memories in scope at all
        'staleness_hole',     -- memories exist but all too old
        'confidence_hole',    -- memories exist but avg confidence too low
        'contradiction_hole'  -- memories contradict each other
    )),
    scope TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    triggered_by TEXT,                          -- query or scan that revealed the gap
    severity REAL NOT NULL DEFAULT 0.5          -- 0.0–1.0
        CHECK(severity >= 0.0 AND severity <= 1.0),
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_gaps_scope ON knowledge_gaps(scope);
CREATE INDEX IF NOT EXISTS idx_gaps_type ON knowledge_gaps(gap_type);
CREATE INDEX IF NOT EXISTS idx_gaps_unresolved ON knowledge_gaps(resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_gaps_severity ON knowledge_gaps(severity DESC) WHERE resolved_at IS NULL;

-- Record this migration
INSERT INTO schema_version (version, description)
VALUES (7, 'Metacognition gap detection — knowledge_coverage and knowledge_gaps tables');
