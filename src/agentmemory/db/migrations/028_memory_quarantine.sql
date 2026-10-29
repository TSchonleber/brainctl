-- Migration 028: Memory immunity system — quarantine table (issue #24)
CREATE TABLE IF NOT EXISTS memory_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    source_trust REAL,
    contradiction_count INTEGER DEFAULT 0,
    quarantined_by TEXT NOT NULL DEFAULT 'system',
    reviewed_by TEXT DEFAULT NULL,
    reviewed_at TEXT DEFAULT NULL,
    verdict TEXT DEFAULT NULL CHECK(verdict IN ('safe','malicious','uncertain')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE INDEX IF NOT EXISTS idx_quarantine_memory_id ON memory_quarantine(memory_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_verdict ON memory_quarantine(verdict);
CREATE INDEX IF NOT EXISTS idx_quarantine_created ON memory_quarantine(created_at DESC);
