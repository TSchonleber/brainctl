-- Migration 006: Timestamp Normalization
-- Normalize all timestamps to ISO 8601 (YYYY-MM-DDTHH:MM:SS) for consistent ORDER BY sorting.
-- Previously, SQLite's datetime('now') wrote space-separated format, while some external writes
-- used ISO 8601 with timezone offsets, causing lexicographic sort failures.
-- See: -- Normalize events.created_at
UPDATE events
SET created_at = substr(created_at, 1, 10) || 'T' || substr(created_at, 12)
WHERE created_at NOT LIKE '%T%';

-- Normalize memories.created_at
UPDATE memories
SET created_at = substr(created_at, 1, 10) || 'T' || substr(created_at, 12)
WHERE created_at NOT LIKE '%T%';

-- Normalize memories.updated_at
UPDATE memories
SET updated_at = substr(updated_at, 1, 10) || 'T' || substr(updated_at, 12)
WHERE updated_at IS NOT NULL AND updated_at NOT LIKE '%T%';

-- Normalize memories.last_recalled_at
UPDATE memories
SET last_recalled_at = substr(last_recalled_at, 1, 10) || 'T' || substr(last_recalled_at, 12)
WHERE last_recalled_at IS NOT NULL AND last_recalled_at NOT LIKE '%T%';

-- Normalize memories.retired_at
UPDATE memories
SET retired_at = substr(retired_at, 1, 10) || 'T' || substr(retired_at, 12)
WHERE retired_at IS NOT NULL AND retired_at NOT LIKE '%T%';

-- Validation triggers: reject non-ISO 8601 timestamps on future writes
CREATE TRIGGER IF NOT EXISTS events_validate_ts_insert
BEFORE INSERT ON events
WHEN NEW.created_at NOT LIKE '____-__-__T%'
BEGIN
  SELECT RAISE(ABORT, 'events.created_at must be ISO 8601 (YYYY-MM-DDTHH:MM:SS)');
END;

CREATE TRIGGER IF NOT EXISTS events_validate_ts_update
BEFORE UPDATE OF created_at ON events
WHEN NEW.created_at NOT LIKE '____-__-__T%'
BEGIN
  SELECT RAISE(ABORT, 'events.created_at must be ISO 8601 (YYYY-MM-DDTHH:MM:SS)');
END;

CREATE TRIGGER IF NOT EXISTS memories_validate_ts_insert
BEFORE INSERT ON memories
WHEN NEW.created_at NOT LIKE '____-__-__T%'
BEGIN
  SELECT RAISE(ABORT, 'memories.created_at must be ISO 8601 (YYYY-MM-DDTHH:MM:SS)');
END;

CREATE TRIGGER IF NOT EXISTS memories_validate_ts_update
BEFORE UPDATE OF created_at ON memories
WHEN NEW.created_at NOT LIKE '____-__-__T%'
BEGIN
  SELECT RAISE(ABORT, 'memories.created_at must be ISO 8601 (YYYY-MM-DDTHH:MM:SS)');
END;
