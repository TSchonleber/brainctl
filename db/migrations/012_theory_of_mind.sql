-- ============================================================
-- Migration 012: Theory of Mind — Agent Mental Models
-- : Theory of Mind & Agent Modeling
-- Author: Weaver (Context Integration Engineer)
-- Date: 2026-03-28
-- ============================================================
-- Adds four tables enabling Hermes to:
--   1. Track what each agent currently believes (agent_beliefs)
--   2. Detect belief conflicts between agents or vs. ground truth (belief_conflicts)
--   3. Model what each agent knows from another agent's perspective (agent_perspective_models)
--   4. Maintain a cached BDI snapshot per agent (agent_bdi_state)
-- ============================================================

INSERT INTO schema_version (version, description)
VALUES (12, 'Theory of Mind: agent_beliefs, belief_conflicts, agent_perspective_models, agent_bdi_state');

-- ============================================================
-- Table 1: agent_beliefs
-- An agent's current belief about a specific topic.
-- Beliefs are agent-local snapshots that may differ from ground
-- truth in memories. The divergence IS the signal.
-- ============================================================
CREATE TABLE agent_beliefs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id            TEXT    NOT NULL REFERENCES agents(id),
    topic               TEXT    NOT NULL,
        -- Scoped topic key, e.g.:
        --   "project:agentmemory:status"
        --   "agent:my-agent:role"
        --   "global:memory_spine:schema_version"
        --   "task: :status"
    belief_content      TEXT    NOT NULL,
    confidence          REAL    NOT NULL DEFAULT 1.0
                            CHECK(confidence >= 0.0 AND confidence <= 1.0),
    source_memory_id    INTEGER REFERENCES memories(id),
    source_event_id     INTEGER REFERENCES events(id),
    is_assumption       INTEGER NOT NULL DEFAULT 0,
        -- 1 = unverified assumption (agent inferred, not explicitly told)
        -- 0 = derived from direct evidence or memory injection
    last_updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    invalidated_at      TEXT,               -- NULL = still believed / active
    invalidation_reason TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(agent_id, topic)
);
CREATE INDEX idx_beliefs_agent      ON agent_beliefs(agent_id);
CREATE INDEX idx_beliefs_topic      ON agent_beliefs(topic);
CREATE INDEX idx_beliefs_active     ON agent_beliefs(invalidated_at) WHERE invalidated_at IS NULL;
CREATE INDEX idx_beliefs_assumption ON agent_beliefs(is_assumption) WHERE is_assumption = 1;
CREATE INDEX idx_beliefs_stale      ON agent_beliefs(last_updated_at);


-- ============================================================
-- Table 2: belief_conflicts
-- Conflicts between agents' beliefs about the same topic,
-- or between an agent's belief and global ground truth.
-- ============================================================
CREATE TABLE belief_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT    NOT NULL,
    agent_a_id      TEXT    NOT NULL REFERENCES agents(id),
    agent_b_id      TEXT    REFERENCES agents(id),
        -- NULL = conflict is with global ground truth (memories), not another agent
    belief_a        TEXT    NOT NULL,   -- what agent A believes
    belief_b        TEXT    NOT NULL,   -- what agent B believes, or ground truth
    conflict_type   TEXT    NOT NULL DEFAULT 'factual'
        CHECK(conflict_type IN (
            'factual',      -- two agents disagree on a fact
            'assumption',   -- one agent is acting on an unverified assumption
            'staleness',    -- one agent's belief is outdated vs. current ground truth
            'scope'         -- agents disagree about ownership or responsibility
        )),
    severity        REAL    NOT NULL DEFAULT 0.5
        CHECK(severity >= 0.0 AND severity <= 1.0),
    detected_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    resolved_at     TEXT,
    resolution      TEXT,
    requires_supervisor_intervention INTEGER NOT NULL DEFAULT 0
        -- 1 = Hermes should inject corrective context before affected agents act
);
CREATE INDEX idx_conflicts_topic    ON belief_conflicts(topic);
CREATE INDEX idx_conflicts_agent_a  ON belief_conflicts(agent_a_id);
CREATE INDEX idx_conflicts_agent_b  ON belief_conflicts(agent_b_id);
CREATE INDEX idx_conflicts_open     ON belief_conflicts(resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX idx_conflicts_severity ON belief_conflicts(severity DESC) WHERE resolved_at IS NULL;
CREATE INDEX idx_conflicts_supervisor ON belief_conflicts(requires_supervisor_intervention)
    WHERE requires_supervisor_intervention = 1 AND resolved_at IS NULL;


-- ============================================================
-- Table 3: agent_perspective_models
-- An observer agent's model of what a subject agent knows.
-- "Observer believes Subject believes X about topic Y."
-- Second-order epistemics — the knowledge-about-knowledge layer.
-- Primary use: Hermes builds perspective models for all agents
-- it routes context to, then uses knowledge_gap to frame context
-- appropriately for the receiver's knowledge state.
-- ============================================================
CREATE TABLE agent_perspective_models (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    observer_agent_id       TEXT    NOT NULL REFERENCES agents(id),
    subject_agent_id        TEXT    NOT NULL REFERENCES agents(id),
    topic                   TEXT    NOT NULL,
    estimated_belief        TEXT,
        -- Observer's best estimate of what subject currently believes.
        -- NULL = observer has no model for this topic (treat as full gap).
    estimated_confidence    REAL
        CHECK(estimated_confidence IS NULL OR (estimated_confidence >= 0.0 AND estimated_confidence <= 1.0)),
        -- How confident is the observer in their estimate of subject's belief?
    knowledge_gap           TEXT,
        -- What observer believes subject does NOT know about this topic.
        -- This is the delta to fill when routing context to subject.
        -- NULL = no known gap (subject likely has sufficient context).
    confusion_risk          REAL    NOT NULL DEFAULT 0.0
        CHECK(confusion_risk >= 0.0 AND confusion_risk <= 1.0),
        -- Probability subject will be confused or err on tasks requiring
        -- knowledge of this topic. Hermes uses this for proactive injection.
        -- Thresholds: > 0.7 = HIGH (inject before routing), 0.4–0.7 = MODERATE
    last_updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(observer_agent_id, subject_agent_id, topic)
);
CREATE INDEX idx_pmodel_observer  ON agent_perspective_models(observer_agent_id);
CREATE INDEX idx_pmodel_subject   ON agent_perspective_models(subject_agent_id);
CREATE INDEX idx_pmodel_topic     ON agent_perspective_models(topic);
CREATE INDEX idx_pmodel_confusion ON agent_perspective_models(confusion_risk DESC);
CREATE INDEX idx_pmodel_gaps      ON agent_perspective_models(knowledge_gap)
    WHERE knowledge_gap IS NOT NULL;


-- ============================================================
-- Table 4: agent_bdi_state
-- Cached BDI (Belief-Desire-Intention) snapshot per agent.
-- Maintained by Hermes maintenance cycle and via
-- `brainctl tom update <agent-id>`.
-- Provides a single-row read path for agent epistemic health.
-- ============================================================
CREATE TABLE agent_bdi_state (
    agent_id                    TEXT    PRIMARY KEY REFERENCES agents(id),

    -- BELIEFS dimension
    beliefs_summary             TEXT,
        -- JSON: {
        --   "active_belief_count": N,
        --   "stale_belief_count": N,       (last_updated > 24h for active-task topics)
        --   "assumption_count": N,          (is_assumption = 1)
        --   "conflict_count": N,            (open belief_conflicts for this agent)
        --   "key_topics": ["t1", "t2", ...]
        -- }
    beliefs_last_updated_at     TEXT,

    -- DESIRES dimension
    desires_summary             TEXT,
        -- JSON: {
        --   "active_task_count": N,
        --   "primary_goal": "...",
        --   "priority": "critical|high|medium|low",
        --   "task_ids": [" ", ...]
        -- }
    desires_last_updated_at     TEXT,

    -- INTENTIONS dimension
    intentions_summary          TEXT,
        -- JSON: {
        --   "in_progress_tasks": [...],
        --   "committed_actions": [...],    (from recent events)
        --   "estimated_completion": "..."
        -- }
    intentions_last_updated_at  TEXT,

    -- EPISTEMIC HEALTH SCORES (0.0–1.0)
    knowledge_coverage_score    REAL,
        -- How well does agent's belief state cover topics required
        -- by their current active tasks? 1.0 = full coverage.
    belief_staleness_score      REAL,
        -- Fraction of active-task beliefs that are stale (>24h).
        -- 1.0 = all beliefs are stale. Target < 0.2.
    confusion_risk_score        REAL,
        -- Aggregate max confusion_risk from agent_perspective_models
        -- where this agent is the subject. 1.0 = high confusion expected.
        -- Hermes triggers proactive injection when this > 0.7.

    last_full_assessment_at     TEXT,
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX idx_bdi_coverage  ON agent_bdi_state(knowledge_coverage_score);
CREATE INDEX idx_bdi_staleness ON agent_bdi_state(belief_staleness_score DESC);
CREATE INDEX idx_bdi_confusion ON agent_bdi_state(confusion_risk_score DESC);
