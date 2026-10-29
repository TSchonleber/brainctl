-- Migration 041: spaced-review scheduler (Cepeda et al. 2006)
ALTER TABLE memories ADD COLUMN next_review_at TEXT DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_next_review
    ON memories(next_review_at) WHERE next_review_at IS NOT NULL AND retired_at IS NULL;
