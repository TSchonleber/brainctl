-- File-anchored memories — link knowledge to specific source files.
-- Inspired by ByteRover's curate pattern: tie context to file paths
-- so retrieval can weight memories by proximity to the active file.

ALTER TABLE memories ADD COLUMN file_path TEXT;  -- e.g. 'src/auth/jwt.ts'
ALTER TABLE memories ADD COLUMN file_line INTEGER;  -- optional: specific line

CREATE INDEX idx_memories_file_path ON memories(file_path) WHERE file_path IS NOT NULL;
