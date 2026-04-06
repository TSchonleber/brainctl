# World Models & Internal Simulation — Can Hermes Simulate the Organization?

**Task:** COS-249
**Author:** Cortex (Intelligence Synthesis Analyst)
**Date:** 2026-03-28
**References:** COS-204 (Memory as Policy Engine), COS-177 (Memory Event Bus), COS-243 (Global Workspace Theory)

---

## Overview

A world model is a compressed, queryable internal representation of an environment
that enables mental simulation: "if we do X, what happens?" This research designs
a **World Model Layer** for CostClock AI — a living model of org structure, agent
capabilities, project dependencies, and causal dynamics that Hermes can query to
predict outcomes before committing resources.

---

## Theoretical Foundations

### Ha & Schmidhuber (2018) — World Models
The "World Models" paper splits cognition into:
- **Controller** — makes decisions
- **Memory (M)** — stores compressed world state (RNN hidden state)
- **Vision (V)** — encodes observations to latent space

Mapping to Hermes:
- Controller = Hermes decision logic
- Memory = brain.db compressed org state
- Vision = the memory ingestion pipeline (brainctl, agents reporting events)

**Key insight:** You don't need a perfect simulation. A compressed, "good enough"
model that captures the most predictive features is sufficient for planning.

### LeCun's Joint Embedding Predictive Architecture (JEPA)
Rather than predicting raw future states (expensive, lossy), JEPA predicts in
abstract latent space. For organizations: instead of predicting "what exact comment
will agent X post," predict "will this task likely complete in 2 heartbeats or 10?"
Prediction at the right level of abstraction.

**Mapping:** The world model predicts _agent behavior categories_ (completes, blocks,
escalates, fails) rather than exact outputs.

### Predictive Processing — Clark (2013)
The brain is a prediction engine. Perception = (top-down prediction) - (bottom-up
error signal). Every moment, the brain predicts what's coming and updates only when
wrong.

**Mapping to Hermes:**
- Hermes continuously maintains a predicted state: "agent X is on track for COS-249
  by EOD"
- When the actual event deviates (X posts a blocked comment), the error is logged
- Over time, Hermes accumulates calibrated models of each agent's behavior

### Pearl's Causal Framework — Structural Causal Models
Correlation-based prediction ("agents who post > 3 comments per task tend to block")
is brittle. Causal models support intervention reasoning: "if I reassign Y to Z,
what changes downstream?"

**Mapping:** The world model should distinguish:
- Observational patterns (`do(X)` = observing X happen naturally)
- Interventional queries (`do(X)` = what if we force X?)

---

## Architecture Design

### The Organizational World Model (OWM)

The OWM is a set of compressed tables in brain.db that together represent the
current state and dynamics of CostClock AI.

```
┌─────────────────────────────────────────────────────────────┐
│                   Organizational World Model                 │
│                                                             │
│  ┌──────────────────┐    ┌───────────────────────────────┐  │
│  │  Static Model     │    │  Dynamics Model               │  │
│  │  (current state)  │    │  (transition probabilities)   │  │
│  │                   │    │                               │  │
│  │  - agent_caps     │    │  - completion_rates           │  │
│  │  - project_deps   │    │  - block_patterns             │  │
│  │  - org_structure  │    │  - escalation_triggers        │  │
│  └──────────────────┘    └───────────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Causal Graph                                        │   │
│  │  (intervention models)                               │   │
│  │                                                      │   │
│  │  "reassigning X increases throughput on Y by ~30%"   │   │
│  │  "tasks with > 5 subtasks have 2x block probability" │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Schema

#### `agent_capabilities` table
```sql
CREATE TABLE agent_capabilities (
    agent_id        TEXT NOT NULL,
    capability      TEXT NOT NULL,       -- e.g., "sql_migration", "research"
    skill_level     REAL DEFAULT 0.5,   -- 0.0-1.0 estimated proficiency
    task_count      INTEGER DEFAULT 0,  -- tasks completed in this domain
    avg_cycles      REAL,               -- avg heartbeats to complete
    block_rate      REAL,               -- historical block rate
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (agent_id, capability)
);
```

#### `project_dependency_graph` table
```sql
CREATE TABLE project_dependency_graph (
    project_id      TEXT NOT NULL,
    depends_on      TEXT NOT NULL,      -- another project_id
    dependency_type TEXT DEFAULT 'soft', -- soft|hard|resource
    strength        REAL DEFAULT 0.5,  -- how tight the coupling is
    derived_from    TEXT,               -- event_ids that support this edge
    PRIMARY KEY (project_id, depends_on)
);
```

#### `world_model_snapshots` table
```sql
CREATE TABLE world_model_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_type   TEXT NOT NULL,      -- 'org_state' | 'prediction' | 'error'
    subject_id      TEXT,               -- agent_id, project_id, task_id
    predicted_state TEXT,               -- JSON
    actual_state    TEXT,               -- JSON (filled in after resolution)
    prediction_error REAL,             -- |predicted - actual|
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);
```

### brainctl Commands (Sketches)

```bash
# Query: what is the predicted outcome for a task assignment?
brainctl sim predict-task --task COS-250 --assignee paperclip-cto

# Query: what if we reassign an agent?
brainctl sim what-if --reassign paperclip-recall --to project agentmemory

# Query: which agent is best suited for a given capability?
brainctl sim best-agent --capability "sql_migration"

# Update: log a prediction error (Hermes calls this after observing actual)
brainctl sim update-error --snapshot-id 42 --actual '{"status":"blocked"}'

# Build: rebuild agent capability table from event history
brainctl sim rebuild-caps
```

---

## Mental Simulation Loop

The key runtime behavior:

```
1. Hermes receives assignment request (or plans proactively)
2. Query OWM: "Given agent X's capability profile and current load,
   what is P(completion | 3 heartbeats)?"
3. OWM returns: distribution over [completes, blocks, needs_help, escalates]
4. Hermes uses this to decide: assign directly vs. create subtasks vs. pair with mentor
5. After task resolves: log actual outcome vs. predicted
6. Update agent_capabilities and world_model_snapshots
7. Over time: prediction errors shrink → model improves
```

This is **predictive processing at the organizational level**: Hermes maintains
priors about each agent, projects them forward, and updates on prediction errors.

---

## Digital Twin Concept

A **digital twin** is an always-synchronized simulation of a real system. For CostClock AI:

- brain.db IS the digital twin of the organization
- Every agent event writes to it
- Hermes queries it to simulate futures
- The twin stays current via the Memory Event Bus (see COS-232)

The key missing piece is the **dynamics layer**: not just what the org looks like
now, but how it moves. That's what `agent_capabilities` and `project_dependency_graph`
provide.

---

## Causal Reasoning — Avoiding the Correlation Trap

A pure correlation model might learn: "tasks assigned to paperclip-sentinel-2 complete
faster." But the cause might be "sentinel-2 is only assigned easy tasks." A causal
model must control for confounders.

**Practical approach for MVP:**
- Track assignment context (task complexity proxy: priority + subtask count)
- Group completion rates by complexity tier, not raw task count
- Use this to produce unconfounded capability estimates

---

## Open Questions

1. **Update frequency:** How often should agent capability scores be recalculated?
   On every event? Batch nightly? Suggest: batch after each completed task.

2. **Cold start:** New agents have no history. Use role-based priors as defaults
   (e.g., a new researcher defaults to the average researcher completion rate).

3. **Adversarial dynamics:** Agents optimizing for high capability scores might
   avoid hard tasks. Need to weight by task difficulty, not just count.

4. **Model drift:** Org structure changes (new agents, new projects) must invalidate
   stale capability estimates. Use `updated_at` + TTL.

5. **Privacy of predictions:** If Hermes tells Agent X "your predicted block rate
   is 40%," does that change behavior (Hawthorne effect)? Keep predictions internal.

---

## Implementation Path

| Phase | Deliverable | Complexity |
|-------|-------------|------------|
| 1 | `agent_capabilities` table + rebuild script from event history | Low |
| 2 | `brainctl sim best-agent` — capability-based routing | Low |
| 3 | `world_model_snapshots` + prediction logging | Medium |
| 4 | `project_dependency_graph` via co-occurrence analysis | Medium |
| 5 | `brainctl sim what-if` — full counterfactual simulation | High |
| 6 | Causal graph construction from controlled experiments | High |

---

## Connections to Other Wave 6 Work

- **COS-243 (Global Workspace Theory):** GWT determines which predictions get
  broadcast. High-error predictions (surprises) should trigger ignition and broadcast.
- **COS-235 (Policy Memory):** Policy memories encode "if we've seen X before, do Y."
  World models encode "if we're about to do X, Y is the likely outcome." Complementary.
- **COS-232 (Memory Event Bus):** All world model updates flow through MEB events.
  Actual vs. predicted outcome logging is a natural MEB event type.
- **COS-231 (Embedding Backfill):** Semantic search over task history is how we
  extract "similar past tasks" for capability estimation.

---

## Summary

A world model for CostClock AI is achievable with four additions to brain.db:
`agent_capabilities`, `project_dependency_graph`, `world_model_snapshots`, and
a prediction-error feedback loop. This transforms Hermes from a reactive coordinator
into a **predictive director** — one that simulates before committing, and learns
from the gap between simulation and reality.
