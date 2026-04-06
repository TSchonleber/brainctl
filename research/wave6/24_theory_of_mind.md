# COS-246: Theory of Mind & Agent Modeling
**Series:** Cognitive Operating System Research
**Wave:** 6
**Report Number:** 24
**Date:** 2026-03-28
**Author:** Weaver (Context Integration Engineer)
**Status:** Complete
**Builds On:** COS-177 (Agent-to-Agent Knowledge Transfer), COS-204 (Memory as Policy Engine),
COS-232 (Memory Event Bus), COS-85 (Coherence / Sentinel)

---

## Summary

This report delivers a concrete schema design for **Theory of Mind (ToM)** capabilities in
`brain.db`. Theory of Mind — the ability to reason about what another agent believes, wants, and
intends — is the missing layer between our current expertise directory (`agent_expertise`) and true
perspective-aware context routing.

We map six cognitive-science frameworks to specific engineering primitives, then propose four new
tables (`agent_beliefs`, `belief_conflicts`, `agent_perspective_models`, `agent_bdi_state`) plus a
`brainctl tom` command interface. The result: Hermes can detect belief conflicts, anticipate agent
confusion, and frame context appropriately for the receiving agent's knowledge state — not just
route memories blindly.

---

## 1. Background: Why ToM Now?

Hermes currently serves ~178 agents. Our memory spine stores ground-truth knowledge in `memories`,
expertise signals in `agent_expertise`, and task state in `tasks`. But none of these answer the
question that determines whether an agent will execute correctly:

> **What does this agent currently believe about the relevant facts?**

An agent can hold a belief that was accurate three days ago but is now stale. When Hermes routes
context without knowing the agent's belief state, it either (a) sends redundant information the
agent already has, or (b) fails to correct a false assumption the agent is still acting on.

The false-belief task (Wimmer & Perner 1983; the "Sally-Anne test") establishes the cognitive
benchmark: an entity with Theory of Mind knows that another agent can hold a belief that differs
from ground truth, and acts accordingly. This is exactly what Hermes needs.

---

## 2. Framework Analysis & Mappings

### 2.1 Theory of Mind (Premack & Woodruff 1978)

**Core claim:** Advanced cognition requires the ability to attribute mental states (beliefs, desires,
intentions) to others, including states that differ from one's own.

**False belief test (Sally-Anne):** Agent Sally places object in Basket. Leaves. Agent Anne moves
object to Box. Sally returns — where does Sally THINK the object is? A ToM-capable agent says
"Basket" (Sally's belief), not "Box" (ground truth).

**Mapping to our system:**
- When Hermes receives a new memory write (via MEB, COS-232), it must check: "Which agents still
  hold the old belief? Their belief is now false."
- The `agent_beliefs` table stores each agent's current belief per topic.
- When ground truth changes, `belief_conflicts` captures the divergence.
- Hermes can then proactively push a corrective context packet before the stale-belief agent acts.

### 2.2 Belief-Desire-Intention (Rao & Georgeff 1995)

**Core claim:** Rational agents can be modeled as having three mental attitudes:
- **Beliefs** — what they take to be true about the world
- **Desires** — end states they want to achieve
- **Intentions** — committed plans currently being executed

**Mapping to our system:**
| BDI Component | brain.db Source | Notes |
|---------------|-----------------|-------|
| Beliefs | `memories` (scoped to agent) + `agent_beliefs` | Raw memories = ground truth; `agent_beliefs` = agent's current derived model |
| Desires | `tasks.assigned_agent_id` + `agent_expertise` | Active tasks = current desires; expertise = preference direction |
| Intentions | `tasks.status = in_progress` + `events` | In-flight tasks = committed intentions; events = the steps being taken |

The `agent_bdi_state` table provides a cached, queryable snapshot of each agent's BDI triple.
Hermes reads this on demand without re-deriving it from scattered tables each time.

### 2.3 Epistemic Logic

**Core claim:** We can reason about knowledge using modal operators: K(a, P) = "agent a knows P";
B(a, P) = "agent a believes P"; and crucially K(a, ¬K(b, P)) = "agent a knows that agent b does
not know P."

**Mapping to our system:**
- `agent_perspective_models` captures second-order beliefs: Hermes's model of what Agent X knows.
- The `knowledge_gap` column is the epistemic gap: what Hermes believes Agent X does NOT know.
- When routing context to Agent X, Hermes queries: "Does my perspective model for X show a gap on
  this topic? If yes, include bridging context."

This prevents the common failure mode where context is routed as-if the receiver has full global
memory access — they don't; they have only what was previously sent to them.

### 2.4 Perspective Taking

**Core claim:** Cognitive empathy — the ability to simulate another entity's viewpoint and
understand what information they would need, framed in terms they understand.

**Mapping to our system:**
- When Hermes prepares a context packet for Agent X, it first queries `agent_perspective_models`
  for Agent X: what does X know? What gaps exist?
- It then frames the context to fill the gap, not to repeat what X already knows.
- The `confusion_risk` score in `agent_perspective_models` provides a routing priority signal:
  high confusion_risk agents get proactive context injection even when not explicitly requested.

### 2.5 Social Cognition in Multi-Agent Systems

**Core claim:** High-performing teams develop shared mental models — common representations of team
goals, individual roles, task requirements, and team processes. Without shared mental models,
coordination degrades.

**Mapping to our system:**
- The `belief_conflicts` table surfaces divergence from the shared mental model.
- Conflict type `scope` = agents disagree on who owns what.
- Conflict type `staleness` = one agent has old beliefs; shared model has drifted.
- Conflict type `assumption` = an agent is operating on an unverified assumption about another
  agent's behavior.
- Hermes monitors `belief_conflicts` in its maintenance cycle. Conflicts with
  `requires_hermes_intervention = 1` trigger a corrective memory push.

### 2.6 Mirror Neurons (Rizzolatti 1996)

**Core claim:** Understanding another entity's actions by internally simulating them. We understand
what someone is doing by activating the same motor representations we would use to perform that
action.

**Mapping to our system:**
- When Hermes sees Agent X struggling (high error rate in `events`, blocked tasks, low
  `agent_bdi_state.knowledge_coverage_score`), it simulates what Agent X is missing.
- This is operationalized as: Hermes takes Agent X's current belief state and runs a gap analysis
  against the memories relevant to X's active task.
- The gap = the context X would need to complete the task.
- Hermes then injects that context proactively via a memory write scoped to `agent:X`.

---

## 3. Schema Design

### Migration 011: Theory of Mind Tables

```sql
-- ============================================================
-- Migration 011: Theory of Mind — Agent Mental Models
-- ============================================================
-- Adds four tables for BDI modeling, belief tracking, conflict
-- detection, and perspective-aware context routing.
-- ============================================================

-- Table 1: agent_beliefs
-- An agent's current belief about a specific topic.
-- Beliefs are agent-local snapshots — they may be stale or
-- contradicted by global memories. This is by design: the gap
-- between beliefs and global truth IS the information.
CREATE TABLE agent_beliefs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id            TEXT    NOT NULL REFERENCES agents(id),
    topic               TEXT    NOT NULL,
        -- Scoped topic key, e.g.:
        --   "project:agentmemory:status"
        --   "agent:hermes:role"
        --   "global:memory_spine:schema_version"
        --   "task:COS-232:status"
    belief_content      TEXT    NOT NULL,   -- what the agent believes
    confidence          REAL    NOT NULL DEFAULT 1.0
                            CHECK(confidence >= 0.0 AND confidence <= 1.0),
    source_memory_id    INTEGER REFERENCES memories(id),
    source_event_id     INTEGER REFERENCES events(id),
    is_assumption       INTEGER NOT NULL DEFAULT 0,
        -- 1 = unverified assumption (agent inferred, not told)
        -- 0 = derived from direct evidence or memory injection
    last_updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    invalidated_at      TEXT,               -- NULL = still believed
    invalidation_reason TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(agent_id, topic)
);
CREATE INDEX idx_beliefs_agent   ON agent_beliefs(agent_id);
CREATE INDEX idx_beliefs_topic   ON agent_beliefs(topic);
CREATE INDEX idx_beliefs_active  ON agent_beliefs(invalidated_at) WHERE invalidated_at IS NULL;
CREATE INDEX idx_beliefs_assumption ON agent_beliefs(is_assumption) WHERE is_assumption = 1;


-- Table 2: belief_conflicts
-- Detected conflicts between agents' beliefs about the same topic,
-- or between an agent's belief and ground truth.
CREATE TABLE belief_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT    NOT NULL,
    agent_a_id      TEXT    NOT NULL REFERENCES agents(id),
    agent_b_id      TEXT    REFERENCES agents(id),
        -- NULL = conflict with global ground truth (memories), not another agent
    belief_a        TEXT    NOT NULL,   -- what agent A believes
    belief_b        TEXT    NOT NULL,   -- what agent B believes, or ground truth
    conflict_type   TEXT    NOT NULL DEFAULT 'factual'
        CHECK(conflict_type IN (
            'factual',      -- two agents disagree on a fact
            'assumption',   -- one agent is acting on an unverified assumption
            'staleness',    -- one agent's belief is based on outdated information
            'scope'         -- agents disagree about ownership/responsibility
        )),
    severity        REAL    NOT NULL DEFAULT 0.5
        CHECK(severity >= 0.0 AND severity <= 1.0),
    detected_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    resolved_at     TEXT,
    resolution      TEXT,
    requires_hermes_intervention INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_conflicts_topic    ON belief_conflicts(topic);
CREATE INDEX idx_conflicts_agent_a  ON belief_conflicts(agent_a_id);
CREATE INDEX idx_conflicts_agent_b  ON belief_conflicts(agent_b_id);
CREATE INDEX idx_conflicts_open     ON belief_conflicts(resolved_at) WHERE resolved_at IS NULL;
CREATE INDEX idx_conflicts_severity ON belief_conflicts(severity DESC) WHERE resolved_at IS NULL;
CREATE INDEX idx_conflicts_hermes   ON belief_conflicts(requires_hermes_intervention)
    WHERE requires_hermes_intervention = 1 AND resolved_at IS NULL;


-- Table 3: agent_perspective_models
-- Hermes's (or any observer agent's) model of what another agent knows.
-- "Observer believes Subject believes X about topic Y."
-- This is second-order epistemics — the knowledge-about-knowledge layer.
CREATE TABLE agent_perspective_models (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    observer_agent_id       TEXT    NOT NULL REFERENCES agents(id),
    subject_agent_id        TEXT    NOT NULL REFERENCES agents(id),
    topic                   TEXT    NOT NULL,
    estimated_belief        TEXT,
        -- Observer's estimate of what Subject currently believes
        -- NULL = observer has no model for this topic
    estimated_confidence    REAL    CHECK(estimated_confidence >= 0.0 AND estimated_confidence <= 1.0),
        -- How confident is the observer in their estimate?
    knowledge_gap           TEXT,
        -- What observer thinks Subject does NOT know about this topic.
        -- This is the delta to fill when routing context to Subject.
    confusion_risk          REAL    NOT NULL DEFAULT 0.0
        CHECK(confusion_risk >= 0.0 AND confusion_risk <= 1.0),
        -- Probability that Subject will be confused or make errors
        -- if they receive a task requiring knowledge of this topic without bridging context.
        -- Hermes uses this for proactive context injection.
    last_updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(observer_agent_id, subject_agent_id, topic)
);
CREATE INDEX idx_pmodel_observer   ON agent_perspective_models(observer_agent_id);
CREATE INDEX idx_pmodel_subject    ON agent_perspective_models(subject_agent_id);
CREATE INDEX idx_pmodel_topic      ON agent_perspective_models(topic);
CREATE INDEX idx_pmodel_confusion  ON agent_perspective_models(confusion_risk DESC);


-- Table 4: agent_bdi_state
-- Cached BDI snapshot per agent. Maintained by Hermes during
-- maintenance cycles and on-demand via `brainctl tom update`.
CREATE TABLE agent_bdi_state (
    agent_id                    TEXT    PRIMARY KEY REFERENCES agents(id),

    -- Beliefs dimension
    beliefs_summary             TEXT,
        -- JSON: {
        --   "active_belief_count": N,
        --   "stale_belief_count": N,
        --   "assumption_count": N,
        --   "conflict_count": N,
        --   "key_topics": ["topic1", "topic2", ...]
        -- }
    beliefs_last_updated_at     TEXT,

    -- Desires dimension
    desires_summary             TEXT,
        -- JSON: {
        --   "active_task_count": N,
        --   "primary_goal": "...",
        --   "priority": "critical|high|medium|low",
        --   "task_ids": ["COS-246", ...]
        -- }
    desires_last_updated_at     TEXT,

    -- Intentions dimension
    intentions_summary          TEXT,
        -- JSON: {
        --   "in_progress_tasks": [...],
        --   "committed_actions": [...],
        --   "estimated_completion": "..."
        -- }
    intentions_last_updated_at  TEXT,

    -- Epistemic health scores
    knowledge_coverage_score    REAL,
        -- 0.0–1.0: How well does this agent's belief state cover
        -- topics relevant to their current tasks?
    belief_staleness_score      REAL,
        -- 0.0–1.0: What fraction of their beliefs are stale?
        -- (belief.last_updated_at is older than 24h for active tasks)
    confusion_risk_score        REAL,
        -- 0.0–1.0: Aggregate confusion risk across perspective models
        -- where this agent is the subject. High = Hermes should inject context.

    last_full_assessment_at     TEXT,
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX idx_bdi_coverage   ON agent_bdi_state(knowledge_coverage_score);
CREATE INDEX idx_bdi_staleness  ON agent_bdi_state(belief_staleness_score DESC);
CREATE INDEX idx_bdi_confusion  ON agent_bdi_state(confusion_risk_score DESC);
```

---

## 4. brainctl tom — Command Interface

```
USAGE
  brainctl -a <agent-id> tom <subcommand> [flags]

SUBCOMMANDS
  update <agent-id>         Refresh BDI state snapshot for an agent.
                            Queries memories, tasks, events to rebuild
                            beliefs_summary, desires_summary, intentions_summary
                            and recompute all three epistemic scores.

  belief set <agent-id> <topic> <content> [--assumption]
                            Record or update a belief for an agent.
                            --assumption marks it as unverified inference.

  belief invalidate <agent-id> <topic> <reason>
                            Mark an agent's belief on a topic as wrong.
                            Creates a belief_conflict record if no conflict exists.

  conflicts list [--agent <id>] [--topic <t>] [--severity <min>]
                            List open belief conflicts, sorted by severity DESC.

  conflicts resolve <id> <resolution>
                            Mark a conflict resolved with explanation.

  perspective set <observer> <subject> <topic> <belief> [--gap <text>] [--confusion <0-1>]
                            Update observer's perspective model of subject on topic.

  perspective get <observer> <subject>
                            Print all perspective model entries for this pair.

  gap-scan <agent-id>       Given agent's active tasks, check their belief state
                            for each required topic. Emit a gap report: which topics
                            are missing, stale, or conflicted.

  inject <agent-id> <topic> Gap-fill: write a memory scoped to agent:<agent-id> covering
                            the topic, and update perspective model to reflect injection.

  status [<agent-id>]       Print BDI health summary. If no agent, prints a ranked table
                            of all agents by confusion_risk_score DESC.
```

### Example: Gap-scan driven proactive injection

```bash
# Hermes maintenance cycle detects Codex is about to work on a task
# that requires knowledge of the new MEB schema (COS-232)
$ brainctl -a hermes tom gap-scan paperclip-codex
TOPIC                              STATUS    STALENESS   CONFUSION_RISK
global:memory_spine:schema_v011    MISSING   —           0.87
project:agentmemory:meb_commands   STALE     72h         0.63
agent:hermes:maintenance_schedule  CURRENT   2h          0.10

# Gap found: inject
$ brainctl -a hermes tom inject paperclip-codex global:memory_spine:schema_v011
→ Memory written: scope=agent:paperclip-codex, category=environment
→ Perspective model updated: observer=hermes, subject=paperclip-codex, topic=...
→ Confusion risk reduced: 0.87 → 0.12
```

---

## 5. Hermes Integration — When to Use ToM

### 5.1 MEB-Triggered Belief Staleness (integrates with COS-232)

When the MEB fires a `memory_events` insert for an important update:

1. Hermes polls `brainctl meb tail --since <watermark>`
2. For each new memory event in scope `global` or `project:*`, check `agent_beliefs` for all
   agents with a belief on the same topic
3. If `belief.last_updated_at < memory_event.created_at`, mark belief as stale
4. If stale belief is for an agent with active in-progress tasks on that topic, create a
   `belief_conflict` with `conflict_type=staleness` and `requires_hermes_intervention=1`
5. In next maintenance cycle, call `tom inject` for each flagged agent

This closes the loop: MEB detects the memory change; ToM detects who is affected; injection
corrects the divergence before the stale-belief agent acts.

### 5.2 Context Routing Filter

Before any context injection or memory routing to Agent X:

```
observed_gap = SELECT knowledge_gap FROM agent_perspective_models
               WHERE observer_agent_id = 'hermes' AND subject_agent_id = X AND topic = T

IF observed_gap IS NOT NULL:
    prepend gap-filling context to injection packet
    frame context assuming Agent X does NOT already know the gap content
ELSE:
    route normally (Agent X likely already has sufficient context)
```

This prevents: "I sent the context but the agent was still confused because they didn't have the
prerequisite knowledge I assumed they had."

### 5.3 Confusion Risk Triage (weekly or on-demand)

```bash
$ brainctl -a hermes tom status
AGENT               COVERAGE   STALENESS   CONFUSION_RISK   STATUS
paperclip-codex     0.43       0.71        0.82             !! HIGH RISK
cortex              0.91       0.08        0.11             OK
recall              0.88       0.12        0.14             OK
lattice             0.52       0.44        0.61             ! MODERATE RISK
engram              0.95       0.05        0.07             OK
```

Agents with `confusion_risk_score > 0.7` are candidates for proactive context injection.
Hermes doesn't wait for them to fail — it fills the gap first.

---

## 6. Worked Example: False Belief Scenario

### Setup
- Memory `#150` says: "brainctl MEB is at migration 010, schema v10"
- Cortex (author of doc 23_policy_memory_schema.md) has belief: `topic=global:meb_schema, belief="schema v10"`
- Weaver just applied migration 011 (this research), writing memory `#155`: "Theory of Mind tables added, schema v11"
- `meb_after_memory_insert` fires → `memory_events` row created

### ToM Processing (Hermes maintenance cycle)

```
1. MEB tail: new event for memory #155, scope=global, category=environment
2. Query agent_beliefs WHERE topic LIKE '%meb_schema%' OR topic LIKE '%schema_v%'
   → cortex has belief: "schema v10" (last_updated 8h ago)
3. Ground truth is now v11. Cortex's belief is a FALSE BELIEF.
4. INSERT belief_conflicts:
     topic=global:memory_spine:schema_version
     agent_a=cortex, agent_b=NULL (ground truth conflict)
     belief_a="schema v10", belief_b="schema v11 (ToM tables)"
     conflict_type=staleness, severity=0.7
     requires_hermes_intervention=1
5. Next heartbeat: tom inject cortex global:memory_spine:schema_version
   → Memory written scoped to agent:cortex with schema v11 summary
   → Cortex's belief updated; conflict resolved
6. If Cortex had tried to act on the old schema BEFORE step 5, the ToM layer
   would have flagged the confusion risk and blocked the context routing.
```

This is the Sally-Anne test, executed in production.

---

## 7. Migration Path

**Phase 1 (now):** Create migration `011_theory_of_mind.sql`. No brainctl commands yet.
Schema exists. Manually writable via raw SQL.

**Phase 2:** Implement `brainctl tom update <agent-id>` — BDI snapshot refresh. Feed it from
existing memories + tasks + events tables. Allows Hermes to build initial BDI states.

**Phase 3:** Implement `brainctl tom gap-scan` + `brainctl tom inject`. This is the payoff:
automated detection and correction of belief gaps before agents act on stale knowledge.

**Phase 4:** Wire MEB → ToM: on every `memory_events` insert, background job checks `agent_beliefs`
for staleness and creates conflicts. This makes the system reactive, not just scheduled.

**Phase 5:** Hermes perspective model maintenance. Each time Hermes routes context to an agent,
update the perspective model: "I told Agent X about topic Y on date Z at confidence C."

---

## 8. Edge Cases & Constraints

| Scenario | Handling |
|----------|----------|
| Agent has no beliefs on a topic | `gap-scan` reports MISSING; treat as `confusion_risk=1.0` for critical topics |
| Two agents both have accurate beliefs | No conflict; both entries in `agent_beliefs` with matching content |
| Hermes itself has a false belief | `observer_agent_id = hermes` in `agent_perspective_models` — Hermes can have wrong ToM too |
| Circular belief chains | `agent_perspective_models` tracks one hop only; Hermes doesn't recurse beyond K(a, B(b, P)) |
| Belief content is long-form | Store hash + reference to memory id; use `source_memory_id` FK |
| 178 agents × N topics = large table | Index on `(agent_id, topic)` UNIQUE; prune invalidated beliefs after 7 days |

---

## 9. Relationship to Prior Wave 6 Research

| Prior doc | Connection |
|-----------|------------|
| COS-177 / doc 20: MEB | MEB triggers are the *detection layer* for belief staleness. ToM is the *response layer*. |
| COS-204 / doc 23: Policy | Policies can reference ToM scores: "IF agent.confusion_risk > 0.8 THEN inject context before routing" |
| COS-85: Coherence (Sentinel) | Sentinel checks global memory coherence; ToM checks per-agent belief coherence. Orthogonal layers. |
| COS-229 / doc 17: Retrieval | `recalled_count` tracking tells us what agents actually retrieved; ToM tracks what they believe they know. Together: complete epistemic picture. |

---

## 10. Definition of Done

- [x] Research document complete with schema design
- [x] All six cognitive science frameworks mapped to concrete primitives
- [x] SQL migration 011 designed and ready for application
- [x] `brainctl tom` command interface specified
- [x] Hermes integration patterns documented (MEB hook, context routing, confusion triage)
- [x] Worked false-belief example (Sally-Anne in production)
- [ ] Migration 011 applied to brain.db (Phase 1 implementation, next task)
- [ ] brainctl tom commands implemented (Phase 2–5)

---

## Appendix: migration 011 file path

`~/agentmemory/db/migrations/011_theory_of_mind.sql`

Contents: the four CREATE TABLE statements and their indexes from section 3 above.
