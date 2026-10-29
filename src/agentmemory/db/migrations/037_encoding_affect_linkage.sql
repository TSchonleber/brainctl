-- Migration 037: encoding affect linkage (Eich & Metcalfe 1989)
ALTER TABLE memories ADD COLUMN encoding_affect_id INTEGER
    REFERENCES affect_log(id) DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_encoding_affect
    ON memories(encoding_affect_id) WHERE encoding_affect_id IS NOT NULL;
