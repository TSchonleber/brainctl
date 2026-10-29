-- Migration 048: FK integrity DELETE triggers + FTS5 retire-aware re-index
--
-- Reversible: yes — `DROP TRIGGER IF EXISTS <name>` per trigger below.
--
-- WHY THIS MIGRATION
-- ------------------
-- The 2026-04-16 correctness audit (memory 1675) found:
--   * Only 2 of 47 migrations declare ON DELETE clauses on FK columns.
--   * No FTS5 DELETE trigger fires when a memory is soft-deleted via
--     `UPDATE memories SET retired_at = ...`, so memories_fts bloats with
--     dangling rows that searches must filter past with WHERE retired_at IS NULL.
--
-- SQLite does NOT support `ALTER TABLE ADD CONSTRAINT`, so retroactively
-- declaring ON DELETE clauses would require rebuilding every affected table.
-- Instead this migration emulates the intended cascade behavior with
-- idempotent DELETE triggers, and converges the FTS5 trigger pair to the
-- packaged-install layout with a `retired_at IS NULL` guard on the insert leg.
--
-- WHEN THE FK CASCADE TRIGGERS HELP
-- ---------------------------------
-- The codebase enforces `PRAGMA foreign_keys = ON` in every connection
-- opener. With FK ON, the SQLite default action (NO ACTION) rejects parent
-- DELETEs that would orphan children — so the trigger paths below are
-- effectively dormant in normal operation.
--
-- They DO fire when:
--   1. Raw SQL maintenance (`sqlite3 brain.db` shell) runs without setting
--      `PRAGMA foreign_keys = ON`.
--   2. `src/agentmemory/merge.py:586` explicitly sets `foreign_keys = OFF`
--      to allow out-of-order INSERTs during ATTACH-style merge.
--
-- SCOPE & DIVERGENCE FROM TASK SPEC
-- ----------------------------------
-- The task asked to "null out memories.validation_agent_id, events.agent_id,
-- decisions.agent_id" on agents hard-delete, and "null out
-- knowledge_edges.source_id WHERE source_table='memories'" (and entities/events).
--
-- Only `memories.validation_agent_id` is nullable; the others (events.agent_id,
-- decisions.agent_id, entities.agent_id, memories.agent_id, and
-- knowledge_edges.source_id/target_id) are all NOT NULL. We can't emulate
-- SET NULL via a trigger on a NOT NULL column — the UPDATE would fail.
--
-- Resolution per column:
--   * Nullable FK → SET NULL via trigger (preserves history row).
--   * NOT NULL FK to memories/entities/events → DELETE the child row.
--     Edges and association rows are meaningless without their referent;
--     cascading DELETE is the only semantically correct option.
--   * NOT NULL agent_id on memories/events/decisions/entities → OUT OF SCOPE.
--     Soft-delete (`agents.status = 'retired'`) remains the contract.
--
-- Tables already covered by ON DELETE CASCADE: recovery_candidates,
-- agent_entanglement, agent_ghz_groups, belief_collapse_events,
-- memory_quarantine, consolidation_forecasts, situation_model_contradictions.
--
-- ===========================================================================
-- Part 1 — FK INTEGRITY DELETE TRIGGERS
-- ===========================================================================

CREATE TRIGGER IF NOT EXISTS trg_agent_delete_nullify_validation
AFTER DELETE ON agents
BEGIN
    UPDATE memories
       SET validation_agent_id = NULL
     WHERE validation_agent_id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_delete_cascade_edges
AFTER DELETE ON memories
BEGIN
    DELETE FROM knowledge_edges
     WHERE (source_table = 'memories' AND source_id = OLD.id)
        OR (target_table = 'memories' AND target_id = OLD.id);
END;

CREATE TRIGGER IF NOT EXISTS trg_entity_delete_cascade_edges
AFTER DELETE ON entities
BEGIN
    DELETE FROM knowledge_edges
     WHERE (source_table = 'entities' AND source_id = OLD.id)
        OR (target_table = 'entities' AND target_id = OLD.id);
END;

CREATE TRIGGER IF NOT EXISTS trg_event_delete_cascade_edges
AFTER DELETE ON events
BEGIN
    DELETE FROM knowledge_edges
     WHERE (source_table = 'events' AND source_id = OLD.id)
        OR (target_table = 'events' AND target_id = OLD.id);
END;

-- ===========================================================================
-- Part 2 — FTS5 retire-aware re-index (audit Item 2)
-- ===========================================================================
-- Goal: when a memory is soft-deleted (UPDATE memories SET retired_at = ...),
-- its FTS5 row must vanish immediately, not bloat the index until the next
-- cmd_vec_purge_retired pass.
--
-- IMPLEMENTATION DETAIL — why this is split-pair convergence, not a new trigger:
--
-- Migration 031 introduced the split-pair pattern (memories_fts_update_delete
-- + memories_fts_update_insert) to support the write-tier `indexed` column.
-- Some existing brain.db files were marked as having 031 applied via
-- `--mark-applied-up-to` (backfilled tracker entry only) so they still carry
-- the OLD single `memories_fts_update` trigger that does
-- delete-then-reinsert in one body. On those DBs, a retire UPDATE re-inserts
-- the row into FTS5 even if the row is then logically retired.
--
-- We initially tried adding a separate `AFTER UPDATE OF retired_at` purge
-- trigger that issued the FTS5 'delete' command. Two problems were verified
-- empirically against the user's live brain.db:
--   1. Plain `DELETE FROM memories_fts WHERE rowid = ?` is NOT supported on
--      a content-linked FTS5 — it silently fails or corrupts segments
--      ("database disk image is malformed").
--   2. Even with the proper 'delete' command idiom, FTS5 statement-level
--      batching means the second 'delete' (from the purge trigger) is
--      no-op'd by the just-issued INSERT (from memories_fts_update). The
--      purge trigger fires AFTER the re-insert, but FTS5 collapses both
--      operations at end-of-statement and the row remains.
--
-- The clean fix is to PREVENT the re-insert at retire time. Splitting the
-- trigger into delete + insert legs and guarding the insert leg with
-- `WHEN ... AND new.retired_at IS NULL` accomplishes that with no
-- second-trigger interference. The packaged init_schema (newer) already
-- ships this layout; this migration converges legacy DBs to match.
--
-- ALL THREE statements below are idempotent:
--   * DROP TRIGGER IF EXISTS memories_fts_update — no-op on packaged-style DBs
--   * DROP TRIGGER IF EXISTS memories_fts_update_insert — drops the old form
--     so the new form (with retired_at guard) takes its place
--   * CREATE TRIGGER IF NOT EXISTS memories_fts_update_delete — no-op if it
--     already exists (packaged); creates it on legacy DBs

DROP TRIGGER IF EXISTS memories_fts_update;
DROP TRIGGER IF EXISTS memories_fts_update_insert;

CREATE TRIGGER IF NOT EXISTS memories_fts_update_delete AFTER UPDATE ON memories WHEN old.indexed = 1 BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
    VALUES ('delete', old.id, old.content, old.category, old.tags);
END;

CREATE TRIGGER memories_fts_update_insert AFTER UPDATE ON memories WHEN new.indexed = 1 AND new.retired_at IS NULL BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags)
    VALUES (new.id, new.content, new.category, new.tags);
END;

-- ===========================================================================
-- Tracker
-- ===========================================================================

INSERT INTO schema_version (version, description, applied_at)
VALUES (48, 'FK integrity DELETE triggers (agents/memories/entities/events) + FTS5 retire-aware re-index (memories_fts_update_insert guarded with retired_at IS NULL)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
