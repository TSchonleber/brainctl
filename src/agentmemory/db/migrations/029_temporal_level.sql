-- Migration 029: Temporal abstraction hierarchy — temporal_level on memories (issue #20)
ALTER TABLE memories ADD COLUMN temporal_level TEXT NOT NULL DEFAULT 'moment'
    CHECK(temporal_level IN ('moment','session','day','week','month','quarter'));

CREATE INDEX IF NOT EXISTS idx_memories_temporal_level ON memories(temporal_level, agent_id);
