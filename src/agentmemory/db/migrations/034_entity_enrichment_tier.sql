-- 034_entity_enrichment_tier.sql
--
-- Add a tiered enrichment signal to the entities table so the consolidation
-- cycle can decide which entities deserve a full refresh versus a cheap
-- observation touch-up.
--
-- Tier 1 — critical: full synthesis + all linked events + compiled_truth rewrite
-- Tier 2 — notable:  compiled_truth refresh + knowledge_edges cleanup
-- Tier 3 — minor:    observation append-only, no synthesis
--
-- Tier is computed by compute_entity_tier() in _impl.py from existing
-- signals (recalled_count on linked memories, knowledge_edges degree, event
-- link count) — no new data source required. The tier column just caches
-- the result so the consolidation pass doesn't have to recompute it on
-- every scan.
--
-- Default tier is 3 so legacy rows don't get promoted until the next
-- consolidation cycle runs `brainctl entity tier --refresh`.

ALTER TABLE entities ADD COLUMN enrichment_tier INTEGER NOT NULL DEFAULT 3;
ALTER TABLE entities ADD COLUMN last_enriched_at TEXT;

-- Index for consolidation scans: "give me all Tier-1 entities whose last
-- enrichment is older than X hours". The partial index keeps it cheap by
-- skipping the overwhelming majority of Tier-3 rows.
CREATE INDEX IF NOT EXISTS idx_entities_tier_enriched
    ON entities(enrichment_tier, last_enriched_at)
    WHERE retired_at IS NULL AND enrichment_tier < 3;
