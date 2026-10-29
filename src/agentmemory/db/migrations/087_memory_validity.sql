-- Migration 087: bi-temporal validity on memories (interference control)
--
-- created_at / retired_at record *transaction* time: when brainctl learned
-- a memory and when it stopped storing it as active. They say nothing about
-- when the fact itself was true. `memory add --supersedes <id>` used to
-- leave the superseded row fully active, so the stale value kept winning
-- searches (it was older, so it won exact-score ties on the lower id).
--
-- valid_from / valid_to record *valid* time: the interval during which the
-- fact held. NULL valid_from means "since created_at"; NULL valid_to means
-- "still true". A supersede of an OPEN / ESTABLISHED incumbent (PII < 0.40)
-- closes its interval (valid_to = the successor's write time) and retires
-- it; the row and the supersedes_id chain stay on disk for audit. An
-- ENTRENCHED / CRYSTALLIZED incumbent stays active: the PII recency gate
-- keeps it in alpha/beta competition with the newcomer. `memory replace`
-- always closes.
--
-- cmd_search drops any candidate whose valid_to has passed, or that an
-- active memory supersedes and whose PII is below ENTRENCHED, so links
-- written before this migration stop intruding too.
--
-- No backfill: whether a legacy predecessor may be closed depends on its
-- PII, which SQL here cannot compute. Search applies the same rule at
-- query time instead. Nothing is retired or deleted here.
--
-- Rollback:
--   DROP INDEX IF EXISTS idx_memories_valid_to;
--   DROP INDEX IF EXISTS idx_memories_supersedes;
--   DELETE FROM schema_versions WHERE version = 87;
--
-- IDEMPOTENT (the runner treats "duplicate column name" as already applied).

ALTER TABLE memories ADD COLUMN valid_from TEXT DEFAULT NULL;
ALTER TABLE memories ADD COLUMN valid_to TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_supersedes
    ON memories(supersedes_id) WHERE supersedes_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_valid_to
    ON memories(valid_to) WHERE valid_to IS NOT NULL;
