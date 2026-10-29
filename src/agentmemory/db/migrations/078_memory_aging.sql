-- Migration 078: memory aging — synaptic tagging-and-capture
--
-- Avenue 2 from research/autonomous-research-avenues-2026-05-20.md.
-- Frey & Morris's synaptic tagging-and-capture hypothesis: memory's
-- late-LTP requires both a TAG (at the synapse during initial encoding)
-- AND plasticity-related proteins (PRPs) showing up within ~1 hour.
--
-- brainctl analog: W(m) gate is the *tag* — "this is plausibly worth
-- keeping". What's missing is the **capture** step that decides
-- whether the memory actually lasts past short-term, conditional on
-- a follow-up signal (typically recall within a critical window).
--
-- Phase 1 ships:
--   memory_tags — per-memory tag with capture deadline + status
--   memory_capture_events — log of capture events (recall, association)
--                           that "consume" the PRP-equivalent
--   memory_aging_state — single-row config (capture_window_hours,
--                        demotion_tier, default decay aggressiveness)
--
-- Phase 1 = inspection + manual tag/capture. Phase 2 auto-tags on
-- memory_add. Phase 3 demotes uncaptured tags to a side tier
-- (memories_unconsolidated). Phase 4 enforces aggressive demotion.
--
-- Rollback:
--   DROP TABLE IF EXISTS memory_capture_events;
--   DROP TABLE IF EXISTS memory_tags;
--   DROP TABLE IF EXISTS memory_aging_state;
--   DELETE FROM schema_version WHERE version = 78;
--
-- IDEMPOTENT.

CREATE TABLE IF NOT EXISTS memory_aging_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    capture_window_hours INTEGER NOT NULL DEFAULT 24 CHECK(capture_window_hours > 0),
    demotion_tier TEXT NOT NULL DEFAULT 'unconsolidated' CHECK(demotion_tier IN (
        'unconsolidated', 'cold_storage', 'retired'
    )),
    enforcement_mode TEXT NOT NULL DEFAULT 'shadow' CHECK(enforcement_mode IN (
        'shadow', 'enforce', 'disabled'
    )),
    total_tags INTEGER NOT NULL DEFAULT 0,
    total_captured INTEGER NOT NULL DEFAULT 0,
    total_demoted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
INSERT OR IGNORE INTO memory_aging_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS memory_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL UNIQUE,
    tagged_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    capture_deadline TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'tagged' CHECK(status IN (
        'tagged', 'captured', 'expired', 'demoted'
    )),
    captured_at TEXT,
    capture_count INTEGER NOT NULL DEFAULT 0,
    demoted_at TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_mt_status ON memory_tags(status, tagged_at);
CREATE INDEX IF NOT EXISTS idx_mt_deadline ON memory_tags(capture_deadline) WHERE status = 'tagged';
CREATE INDEX IF NOT EXISTS idx_mt_memory ON memory_tags(memory_id);

CREATE TABLE IF NOT EXISTS memory_capture_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    memory_id INTEGER NOT NULL,
    tag_id INTEGER REFERENCES memory_tags(id) ON DELETE SET NULL,
    capture_kind TEXT NOT NULL CHECK(capture_kind IN (
        'recall', 'reconsolidation', 'association', 'manual_capture', 'other'
    )),
    agent_id TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_mce_recent ON memory_capture_events(captured_at);
CREATE INDEX IF NOT EXISTS idx_mce_kind ON memory_capture_events(capture_kind, captured_at);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (78, 'memory aging Phase 1: synaptic tagging-and-capture (3 tables, shadow mode default)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
