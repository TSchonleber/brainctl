-- Migration 025: PII audit trail column -- Records which memory was superseded when the PII recency gate was applied.
-- Non-blocking: column is nullable, existing rows are unaffected.

ALTER TABLE memories ADD COLUMN gated_from_memory_id INTEGER REFERENCES memories(id);
