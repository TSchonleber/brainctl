-- Migration 047: Attention Economics Phase 1 — attention_class column
-- (RENUMBERED from original 021 in v2.2.0 — see migrate.py header
-- comment "DUPLICATE-VERSION HISTORY" for context. This pair was not
-- in the original bug report but was caught by the new dupe detector.)
-- Add attention_class to agents table for cognitive budget tiering.
-- Values: 'exec' | 'ic' | 'peripheral' | 'dormant'
-- Ref: ~/agentmemory/research/wave10/28_attention_economics.md Sections 4-5
--
-- IDEMPOTENT: ADD COLUMN is guarded at runtime by migrate.py
-- (`_apply_sql` strips out duplicate-column failures so re-applying
-- is safe). SQLite has no native `ADD COLUMN IF NOT EXISTS` syntax.

ALTER TABLE agents ADD COLUMN attention_class TEXT NOT NULL DEFAULT 'ic';

-- Tier promotion based on attention_class. Moved here from 023_attention_budget
-- in v2.2.0 because the rename of 021_attention_class -> 047 means
-- attention_class is no longer populated when 023 runs. Wrapping each UPDATE
-- in a guard so the migration is safe to re-apply: the WHERE clause never
-- finds rows whose attention_class doesn't exist, but if attention_budget_tier
-- has already been backfilled by an earlier run we still want subsequent
-- changes (new exec/peripheral/dormant agents) to land their tier.

UPDATE agents SET attention_budget_tier = 0 WHERE attention_class = 'exec' AND attention_budget_tier <> 0;
UPDATE agents SET attention_budget_tier = 2 WHERE attention_class = 'peripheral' AND attention_budget_tier <> 2;
UPDATE agents SET attention_budget_tier = 3 WHERE attention_class = 'dormant' AND attention_budget_tier <> 3;

INSERT INTO schema_version (version, description, applied_at)
VALUES (47, 'attention_class column on agents + tier promotion (renumbered from 021; tier UPDATEs moved here from 023)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
