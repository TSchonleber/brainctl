-- 033_entity_compiled_truth.sql
--
-- Add a queryable "compiled truth" surface to the entities table: a single
-- synthesised block that callers can read instead of reassembling an entity
-- from its observations + linked memories + events on every read.
--
-- Pattern is a two-layer separation:
--   * compiled_truth      — current best understanding, rewritten by the
--                           consolidation cycle whenever new evidence lands.
--   * observations + events — append-only evidence log (already present in
--                             the existing schema). Never rewritten.
--
-- Background: brainctl already has `supersedes_id` on memories and a
-- knowledge_edges graph, but there is no single "current synthesis" field on
-- the entity itself. Agents either pay the cost of walking the graph on every
-- lookup, or they re-derive the synthesis from raw observations using their
-- own prompting — both waste tokens and produce inconsistent answers. This
-- migration lets the consolidation pass precompute the answer once.
--
-- Added columns:
--   compiled_truth            TEXT   — synthesised paragraph, UTF-8
--   compiled_truth_updated_at TEXT   — ISO8601 timestamp of last rewrite
--   compiled_truth_source     TEXT   — JSON array of source ids the rewrite
--                                      drew from, e.g.
--                                      ["mem:123","mem:456","evt:78"]
--
-- All three are nullable; legacy rows without compiled_truth continue to work.
-- No CHECK constraints so the rewriter can emit empty strings or structured
-- JSON later without another migration.

ALTER TABLE entities ADD COLUMN compiled_truth TEXT;
ALTER TABLE entities ADD COLUMN compiled_truth_updated_at TEXT;
ALTER TABLE entities ADD COLUMN compiled_truth_source TEXT;

-- Index on the updated_at column so the consolidation cycle can quickly
-- find entities whose compiled_truth is stale relative to their most
-- recent observations / linked memories.
CREATE INDEX IF NOT EXISTS idx_entities_compiled_truth_updated_at
    ON entities(compiled_truth_updated_at);
