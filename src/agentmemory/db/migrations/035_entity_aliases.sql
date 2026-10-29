-- 035_entity_aliases.sql
--
-- Add a first-class aliases column to the entities table so the merger can
-- catch canonical-name collisions *before* it spends an embedding on
-- semantic dedup. Aliases are stored as a JSON array of strings — keeps
-- the schema simple and the lookup cheap for the usual cardinalities.
--
-- Examples:
--   entities.name = 'Alice Chen'
--   entities.aliases = '["A. Chen", "alice@example.com", "alicec"]'
--
-- Used by:
--   * find_entity_by_alias() — entity.create / merge pre-check
--   * `brainctl entity alias add|remove|list` — CLI surface
--
-- Default is NULL so the existing entity read paths don't need to care
-- until callers opt in; the merger falls back to semantic dedup when
-- aliases is NULL.

ALTER TABLE entities ADD COLUMN aliases TEXT;

-- Ad-hoc lookup index on entities(aliases) isn't useful since SQLite can't
-- index into JSON arrays without a virtual column. Keep the query path
-- simple: a small SELECT ... WHERE aliases IS NOT NULL pass in
-- find_entity_by_alias is plenty for any realistic entity count.
