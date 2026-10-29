-- Migration 049: affect_log retention indexes
--
-- Audit (2.2.0 slow-bleed): hourly affect logging with no retention policy
-- yields millions of rows over time. The 2.2.3 patch wave adds an explicit
-- `brainctl affect prune` CLI for time-based / row-count-based retention.
--
-- This migration adds an index on affect_log.created_at for cross-agent
-- time-range deletes. The existing idx_affect_agent_time(agent_id,
-- created_at DESC) cannot serve a `WHERE created_at < ?` predicate that
-- spans all agents — its leading column is agent_id. The new index is
-- standalone on created_at and lets `brainctl affect prune --days N`
-- run as a single ranged DELETE without a full table scan.
--
-- IDEMPOTENT: IF NOT EXISTS guards re-application; insert into schema_version
-- is INSERT OR IGNORE so a re-run does not collide on the version PK.

CREATE INDEX IF NOT EXISTS idx_affect_created_at ON affect_log(created_at);

INSERT OR IGNORE INTO schema_version (version, description, applied_at)
VALUES (49, 'affect_log.created_at index for cross-agent retention prune (2.2.3)',
        strftime('%Y-%m-%dT%H:%M:%S', 'now'));
