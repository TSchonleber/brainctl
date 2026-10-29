-- Migration 019: Reflexion Cross-Agent Propagation -- Author: Sentinel 2 (Memory Integrity Monitor)
-- Date: 2026-03-28
-- Purpose: Add propagated_to column to reflexion_lessons to track which agents have
--          already received a propagated copy of each lesson (idempotency guard).
--          Also adds propagation_source_lesson_id to track the original lesson a copy
--          was generalized from.
-- References: (Reflexion Failure Taxonomy spec), -- Schema version: 18 -> 19

-- Track which agent IDs this lesson has already been propagated to (JSON array of agent_id strings).
-- Prevents duplicate propagation on repeated consolidation cycles.
ALTER TABLE reflexion_lessons ADD COLUMN propagated_to TEXT NOT NULL DEFAULT '[]';

-- For propagated copies: the source lesson ID this was generalized from.
ALTER TABLE reflexion_lessons ADD COLUMN propagation_source_lesson_id INTEGER REFERENCES reflexion_lessons(id);

-- Index for efficient propagation queries (find lessons with non-empty propagation scope)
CREATE INDEX idx_rlessons_propagated ON reflexion_lessons(propagated_to)
    WHERE propagated_to != '[]';

-- Index for tracing propagation lineage
CREATE INDEX idx_rlessons_prop_source ON reflexion_lessons(propagation_source_lesson_id)
    WHERE propagation_source_lesson_id IS NOT NULL;

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (19, datetime('now'),
    'reflexion_lessons.propagated_to + propagation_source_lesson_id — cross-agent lesson propagation ');

PRAGMA user_version = 19;
