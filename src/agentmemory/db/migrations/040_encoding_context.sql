-- Migration 040: encoding context snapshot (Tulving & Thomson 1973)
ALTER TABLE memories ADD COLUMN encoding_task_context TEXT DEFAULT NULL;
ALTER TABLE memories ADD COLUMN encoding_context_hash TEXT DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_context_hash
    ON memories(encoding_context_hash) WHERE encoding_context_hash IS NOT NULL;
