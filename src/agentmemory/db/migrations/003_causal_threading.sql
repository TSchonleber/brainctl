-- Migration 003: Causal threading for events
-- Adds caused_by_event_id and causal_chain_root columns to events table
-- Schema version: 2 -> 3

ALTER TABLE events ADD COLUMN caused_by_event_id INTEGER REFERENCES events(id);
ALTER TABLE events ADD COLUMN causal_chain_root INTEGER REFERENCES events(id);

CREATE INDEX idx_events_caused_by ON events(caused_by_event_id);
CREATE INDEX idx_events_causal_root ON events(causal_chain_root);

PRAGMA user_version = 3;
