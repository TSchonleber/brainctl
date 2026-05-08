-- 052_cognitive_profile.sql
--
-- Cognitive profile feature (autistic-brain features). Adds a per-agent
-- `cognitive_profile` selector and the schema surfaces needed to support
-- the `autistic` profile alongside the default `neurotypical` profile.
--
-- Default behavior is preserved everywhere: every existing row gets
-- 'neurotypical' on backfill, and the profile resolver falls back to
-- neurotypical defaults when the column is NULL or missing.
--
-- The autistic profile retunes existing brainctl machinery rather than
-- forking it. The cognitive theories grounding the retuning are
-- documented in docs/AUTISTIC_BRAIN.md (HIPPEA, monotropism, weak
-- central coherence, hypo-priors). The numbers themselves live in
-- src/agentmemory/cognitive_profile.py — this migration only adds the
-- columns/indexes that those numbers need to act on.
--
-- Added columns:
--   agents.cognitive_profile             — 'neurotypical' (default) | 'autistic'
--   entities.compiled_truth_variants     — JSON array of distinct descriptions
--                                          when contradictions are preserved
--                                          rather than smoothed (WCC)
--   entities.contradiction_count         — running count of preserved variants,
--                                          for fast filtering / inspection
--   entities.special_interest            — 0/1 — first-class monotropism tag
--   entities.interest_strength           — 0.0-1.0 — depth of focus, used by
--                                          retention boosts and --focus rerank
--   affect_log.sensory_load              — 0.0-1.0 composite (max across channels)
--                                          for cheap overload threshold checks
--   affect_log.sensory_dimensions        — JSON {auditory, visual, tactile,
--                                          proprioceptive, interoceptive} per-channel
--
-- IDEMPOTENT: ALTER ADD COLUMN errors are caught by the migration runner
-- (_apply_sql tolerates duplicate-column failures); CREATE INDEX uses
-- IF NOT EXISTS; the schema_version insert is OR IGNORE so re-runs are
-- safe. No data backfill is needed — all defaults are NULL or 0.

ALTER TABLE agents ADD COLUMN cognitive_profile TEXT NOT NULL DEFAULT 'neurotypical';

CREATE INDEX IF NOT EXISTS idx_agents_cognitive_profile
    ON agents(cognitive_profile);

-- Entity contradiction preservation (autistic profile = WCC + literal recall).
-- compiled_truth_variants stores [{text, source_ids, recorded_at}, ...]
-- without smoothing the contradictory ones into the single compiled_truth
-- field. Neurotypical profile leaves this NULL and uses compiled_truth as
-- before.
ALTER TABLE entities ADD COLUMN compiled_truth_variants TEXT;
ALTER TABLE entities ADD COLUMN contradiction_count INTEGER NOT NULL DEFAULT 0;

-- Special-interest tagging — first-class concept for monotropism.
-- entities.special_interest = 1 → retention boosts, retire-resistance,
-- and --focus retrieval mode amplification (rerank profile #7).
ALTER TABLE entities ADD COLUMN special_interest INTEGER NOT NULL DEFAULT 0;
ALTER TABLE entities ADD COLUMN interest_strength REAL NOT NULL DEFAULT 0.0;

CREATE INDEX IF NOT EXISTS idx_entities_special_interest
    ON entities(special_interest, interest_strength DESC)
    WHERE special_interest = 1;

-- Sensory dimensions on affect_log. Keep VAD untouched; add a single
-- composite REAL for cheap threshold checks (overload at sensory_load
-- > profile.sensory_overload_threshold) plus full per-channel JSON for
-- diagnostics. Both nullable — neurotypical writes leave them NULL.
ALTER TABLE affect_log ADD COLUMN sensory_load REAL;
ALTER TABLE affect_log ADD COLUMN sensory_dimensions TEXT;

CREATE INDEX IF NOT EXISTS idx_affect_sensory_load
    ON affect_log(agent_id, sensory_load DESC)
    WHERE sensory_load IS NOT NULL;

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (52,
        'cognitive_profile: agents.cognitive_profile + entity contradiction variants + special-interest tagging + affect_log sensory dimensions (autistic-brain features)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
