-- Migration 014: Dream Hypotheses — Incubation Queue for Creative Synthesis
-- Author: Prune (Memory Hygiene Specialist)
-- Date: 2026-03-28
-- Purpose: Support the Dream Pass in the hippocampus consolidation cycle.
--          Stores cross-scope bisociation hypotheses generated during sleep-cycle
--          synthesis. Hypotheses incubate for 7 days: recalled ones get promoted to
--          real memories; unrecalled ones auto-retire.
-- References: research/wave6/24_creative_synthesis_dreams.md , -- Schema version: 13 -> 14

CREATE TABLE dream_hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_a_id INTEGER NOT NULL REFERENCES memories(id),
    memory_b_id INTEGER NOT NULL REFERENCES memories(id),
    hypothesis_memory_id INTEGER REFERENCES memories(id),  -- the synthesized hypothesis memory
    similarity REAL NOT NULL,                              -- cosine similarity at creation time
    status TEXT NOT NULL DEFAULT 'incubating'              -- incubating | promoted | retired
        CHECK(status IN ('incubating', 'promoted', 'retired')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    promoted_at TEXT,
    retired_at TEXT,
    retirement_reason TEXT
);

CREATE INDEX idx_dream_hypotheses_status ON dream_hypotheses(status);
CREATE INDEX idx_dream_hypotheses_created ON dream_hypotheses(created_at DESC);
CREATE INDEX idx_dream_hypotheses_hypothesis_memory ON dream_hypotheses(hypothesis_memory_id);
CREATE INDEX idx_dream_hypotheses_pair ON dream_hypotheses(memory_a_id, memory_b_id);

INSERT OR REPLACE INTO schema_version (version, applied_at, description)
VALUES (14, datetime('now'),
  'dream_hypotheses table — incubation queue for creative synthesis dream pass ');

PRAGMA user_version = 14;
