-- Migration 004: Cognitive Experiments & Self-Improvement Tracking
-- Author: Cortex (Intelligence Synthesis Analyst)
-- Date: 2026-03-28
-- Purpose: Track Hermes/Cortex cognitive experiments for continuous self-improvement

-- =============================================================================
-- COGNITIVE_EXPERIMENTS — formal experiment tracking
-- Each row = one hypothesis tested about memory system behavior
-- =============================================================================

CREATE TABLE IF NOT EXISTS cognitive_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,                       -- short slug, e.g. "hybrid-bm25-vector-rrf"
    hypothesis TEXT NOT NULL,                        -- what we believe will happen
    implementation_change TEXT,                      -- what was actually changed (SQL, code, config)
    status TEXT NOT NULL DEFAULT 'proposed'          -- proposed | active | completed | abandoned
        CHECK (status IN ('proposed', 'active', 'completed', 'abandoned')),
    led_by_agent TEXT REFERENCES agents(id),         -- primary experimenter
    started_at TEXT,
    completed_at TEXT,
    -- Metrics (stored as JSON to allow flexible before/after comparison)
    baseline_metrics TEXT,                           -- JSON: {"retrieval_p@5": 0.62, "avg_latency_ms": 45}
    outcome_metrics TEXT,                            -- JSON: same keys after experiment
    outcome TEXT,                                    -- 'success' | 'partial' | 'failure' | 'inconclusive'
    outcome_summary TEXT,                            -- human-readable result
    lesson TEXT,                                     -- durable takeaway stored back to memory system
    -- Meta
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_experiments_status ON cognitive_experiments(status);
CREATE INDEX idx_experiments_agent ON cognitive_experiments(led_by_agent);
CREATE INDEX idx_experiments_outcome ON cognitive_experiments(outcome);

-- =============================================================================
-- SELF_ASSESSMENTS — periodic quality evaluations
-- Run on a cadence (daily/weekly) to score memory system performance
-- =============================================================================

CREATE TABLE IF NOT EXISTS self_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessed_by TEXT REFERENCES agents(id),
    assessment_period_start TEXT NOT NULL,
    assessment_period_end TEXT NOT NULL,
    -- Core quality dimensions (0.0–1.0)
    retrieval_relevance REAL,    -- Are retrieved memories relevant to queries?
    forgetting_quality REAL,     -- Are we forgetting the right things?
    retention_quality REAL,      -- Are we keeping the right things?
    context_speed REAL,          -- How fast is context injection? (normalized)
    routing_accuracy REAL,       -- Are memory categories/scopes accurate?
    coherence_score REAL,        -- Are memories internally consistent (no contradictions)?
    -- Failure analysis
    failure_categories TEXT,     -- JSON: {"wrong_category": 3, "retrieval_miss": 7, "stale_data": 2}
    top_failure_type TEXT,       -- Most common failure category this period
    improvement_priority TEXT,   -- What to fix first
    -- Notes
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_assessments_agent ON self_assessments(assessed_by);
CREATE INDEX idx_assessments_time ON self_assessments(assessment_period_end DESC);

-- =============================================================================
-- Update schema_version
-- =============================================================================

INSERT OR REPLACE INTO schema_version(version, applied_at, description)
VALUES (4, datetime('now'), 'cognitive_experiments and self_assessments tables');
