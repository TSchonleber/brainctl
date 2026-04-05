# Temporal Reasoning & Causal Inference
## Research Report — COS-114
**Author:** Epoch (Temporal Cognition Engineer)
**Date:** 2026-03-28
**Target:** brain.db — Temporal reasoning layer enabling causal understanding for Hermes and 178+ agents

---

## Executive Summary

This report investigates how to give Hermes genuine temporal reasoning and causal inference — moving beyond "what happened when" to "why did this happen, what caused what, and what would have happened otherwise." Six theoretical frameworks are analyzed against brain.db's SQLite-first architecture. The central finding: **event calculus over the existing `events` table, combined with lightweight causal DAG construction from temporal co-occurrence patterns, provides the highest-impact capability at lowest implementation cost.** Full Pearl do-calculus requires intervention data we don't yet collect; Granger causality requires dense, regular time series we don't have. Both are deferred to Phase 2.

**Highest-impact recommendations:**
1. Implement event calculus predicates as SQL views + `brainctl temporal query` command (3-4 days)
2. Build causal edge auto-detection over events table using temporal proximity + co-occurrence heuristics (4-5 days)
3. Add a temporal query language layer for "before/after/during/because-of" queries (2-3 days)
4. Prototype counterfactual estimation via decision-point logging (Phase 2, requires schema change)

---

## 1. Event Calculus — Formal Temporal Reasoning

### Theory — Kowalski & Sergot (1986)

**Core idea:** Events *initiate* and *terminate* fluents (time-varying properties). At any timepoint, a fluent holds if it was initiated by some past event and not subsequently terminated. This gives a formal, queryable model of "what is true now and why."

**Key paper:** Kowalski, R. & Sergot, M. (1986). "A logic-based calculus of events." *New Generation Computing*, 4(1), 67-95.

**The predicates:**
```
Happens(event, time)           — event occurred at time
Initiates(event, fluent, time) — event starts a property being true
Terminates(event, fluent, time)— event stops a property being true
HoldsAt(fluent, time)          — property is true at time

HoldsAt(f, t) iff ∃e,t1: Happens(e,t1) ∧ t1 < t ∧ Initiates(e,f,t1)
              ∧ ¬∃e2,t2: Happens(e2,t2) ∧ t1 < t2 < t ∧ Terminates(e2,f,t2)
```

### brain.db Application

The `events` table already records `Happens(event, time)`:
```sql
events(id, type, summary, agent_id, project, tags, refs, created_at, ...)
```

**What's missing:** Fluent definitions and initiation/termination mappings. We need:

1. A `fluents` table (or view) that defines time-varying properties
2. Rules mapping event types to fluent state changes
3. A `HoldsAt` query that computes current state from event history

### Proposed Schema Extension

```sql
-- Fluent definitions: things that can be true or false over time
CREATE TABLE IF NOT EXISTS temporal_fluents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,               -- e.g., 'agent:epoch:status:active'
    category TEXT NOT NULL,           -- 'agent_state', 'project_state', 'system_state'
    description TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Rules: which events initiate/terminate which fluents
CREATE TABLE IF NOT EXISTS temporal_rules (
    id INTEGER PRIMARY KEY,
    event_type_pattern TEXT NOT NULL,  -- glob pattern matching events.type
    fluent_name_template TEXT NOT NULL,-- template with {agent_id}, {project} placeholders
    effect TEXT NOT NULL CHECK(effect IN ('initiates', 'terminates')),
    priority INTEGER DEFAULT 0,       -- higher priority rules override lower
    condition_sql TEXT,               -- optional SQL predicate on the event row
    created_at TEXT DEFAULT (datetime('now'))
);

-- Materialized fluent state (optional, for performance)
CREATE TABLE IF NOT EXISTS temporal_state (
    fluent_id INTEGER REFERENCES temporal_fluents(id),
    holds INTEGER NOT NULL DEFAULT 1, -- 1=true, 0=false
    since_event_id INTEGER REFERENCES events(id),
    since_time TEXT NOT NULL,
    computed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (fluent_id)
);
```

### Example Rules

```sql
-- "Agent checkout" initiates "agent is working on issue"
INSERT INTO temporal_rules VALUES (NULL,
    'issue.checkout',
    'agent:{agent_id}:working_on:{ref_issue_id}',
    'initiates', 0, NULL, datetime('now'));

-- "Issue status done" terminates "agent is working on issue"
INSERT INTO temporal_rules VALUES (NULL,
    'issue.status.done',
    'agent:{agent_id}:working_on:{ref_issue_id}',
    'terminates', 0, NULL, datetime('now'));

-- "Deploy" initiates "version X is live"
INSERT INTO temporal_rules VALUES (NULL,
    'deploy.*',
    'system:deploy:{project}:live',
    'initiates', 0, NULL, datetime('now'));
```

### HoldsAt Query (SQL Implementation)

```sql
-- Does fluent F hold at time T?
-- Find the most recent event that either initiates or terminates F before T
WITH fluent_events AS (
    SELECT e.id, e.created_at, r.effect,
           ROW_NUMBER() OVER (ORDER BY e.created_at DESC) as rn
    FROM events e
    JOIN temporal_rules r ON e.type GLOB r.event_type_pattern
    WHERE r.fluent_name_template = :fluent_name
      AND e.created_at <= :query_time
    ORDER BY e.created_at DESC
    LIMIT 1
)
SELECT CASE WHEN effect = 'initiates' THEN 1 ELSE 0 END as holds,
       created_at as since
FROM fluent_events
WHERE rn = 1;
```

### brainctl Integration

```bash
# "What fluents hold right now?"
brainctl temporal state

# "When did agent Epoch last become active?"
brainctl temporal query "agent:epoch:status:active" --since

# "What was true at time T?"
brainctl temporal snapshot --at "2026-03-27T14:00:00Z"

# "What changed between T1 and T2?"
brainctl temporal diff --from "2026-03-27T00:00:00Z" --to "2026-03-28T00:00:00Z"
```

---

## 2. Causal Inference — Pearl's Framework

### Theory — Pearl (2000, 2009)

**Core idea:** Causation is not correlation. A Structural Causal Model (SCM) is a DAG where edges represent direct causal influence, not mere association. The do-calculus distinguishes *observing* X=x from *intervening* to set X=x.

**Key papers:**
- Pearl, J. (2009). *Causality: Models, Reasoning, and Inference.* Cambridge University Press.
- Pearl, J. (2000). "Causality: Models, Reasoning, and Inference." Cambridge.

**The framework:**
```
SCM: {U, V, F}
  U = exogenous variables (unobserved causes)
  V = endogenous variables (observed)
  F = structural equations: v_i = f_i(parents(v_i), u_i)

do-calculus rules:
  P(Y | do(X=x)) ≠ P(Y | X=x) in general
  Backdoor criterion: adjust for confounders Z
  P(Y | do(X=x)) = Σ_z P(Y | X=x, Z=z) P(Z=z)
```

### brain.db Application — Feasibility Assessment

**Challenge:** Pearl's framework requires either:
1. Known causal structure (a DAG we specify), or
2. Intervention data (experiments where we forced certain actions)

For agent event logs, we have neither by default. We observe correlations: "whenever Agent A deploys, Agent B's error rate goes up." But confounders abound (both might respond to the same external trigger).

**Practical path forward:**
1. **Phase 1 (now):** Build causal DAGs from domain knowledge + temporal ordering. An event that precedes another and is semantically related is a *candidate cause*. Store these as `causes` edges in `knowledge_edges` with confidence scores.
2. **Phase 2 (future):** Introduce intervention logging. When an agent makes a deliberate decision (choosing action A over B), log the counterfactual ("chose A; B was the alternative"). This creates the intervention data needed for genuine do-calculus.

### Decision-Point Logging (Phase 2 Schema)

```sql
CREATE TABLE IF NOT EXISTS decision_points (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),  -- the event recording the chosen action
    agent_id TEXT NOT NULL,
    context_summary TEXT,                     -- what the agent knew at decision time
    chosen_action TEXT NOT NULL,              -- what was done
    alternatives TEXT,                        -- JSON array of considered alternatives
    rationale TEXT,                           -- why this was chosen
    outcome_event_ids TEXT,                   -- JSON array of consequent event IDs (filled later)
    outcome_quality REAL,                     -- retrospective assessment (-1 to 1)
    created_at TEXT DEFAULT (datetime('now'))
);
```

This enables genuine counterfactual queries: "In 5 similar situations, agents chose X three times and Y twice. X-outcomes scored 0.7 average; Y-outcomes scored 0.3. X causally produces better outcomes in this context."

---

## 3. Temporal Knowledge Graphs — Bitemporal Modeling

### Theory

Temporal knowledge graphs extend static KGs with two time dimensions:

1. **Valid time (Tv):** When the fact was true in the world
2. **Transaction time (Tt):** When the fact was recorded in the system

**Key references:**
- Snodgrass, R.T. (1999). *Developing Time-Oriented Database Applications in SQL.*
- Leblay, J. & Chekol, M.W. (2018). "Deriving validity time in knowledge graphs." *WWW Companion.*

**Why bitemporal matters for agents:**
- An agent records "Project Alpha is on track" at T=Monday (Tt=Monday).
- At T=Friday, we learn Project Alpha was actually off-track since Wednesday (Tv=Wednesday, but Tt=Friday).
- Bitemporal modeling lets us answer: "What did we *believe* on Thursday?" (Tt-query) vs. "What was *actually true* on Thursday?" (Tv-query).

### brain.db Application

The `memories` table already has `created_at` (≈ transaction time). **What's missing:** valid-time scoping.

```sql
-- Proposed addition to memories table
ALTER TABLE memories ADD COLUMN valid_from TEXT;  -- when the fact became true
ALTER TABLE memories ADD COLUMN valid_until TEXT;  -- when the fact stopped being true (NULL = still true)
```

With these columns, we can answer:
```sql
-- "What did we believe was true at time T?" (transaction-time query)
SELECT * FROM memories WHERE created_at <= :T AND (retired_at IS NULL OR retired_at > :T);

-- "What was actually true at time T?" (valid-time query)
SELECT * FROM memories
WHERE valid_from <= :T AND (valid_until IS NULL OR valid_until > :T)
  AND retired_at IS NULL;

-- "What did we believe on Monday about the state of things on Wednesday?"
SELECT * FROM memories
WHERE created_at <= :monday
  AND valid_from <= :wednesday
  AND (valid_until IS NULL OR valid_until > :wednesday);
```

### Temporal Scoping for knowledge_edges

Edges also need temporal validity. The relationship "A supports B" might only hold during a certain period.

```sql
ALTER TABLE knowledge_edges ADD COLUMN valid_from TEXT;
ALTER TABLE knowledge_edges ADD COLUMN valid_until TEXT;
```

---

## 4. Granger Causality — Statistical Temporal Inference

### Theory — Granger (1969)

**Core idea:** X "Granger-causes" Y if past values of X help predict Y better than Y's past alone. It's not true causation (it's predictive precedence), but it's useful for discovering temporal dependencies in time series.

**Key paper:** Granger, C.W.J. (1969). "Investigating causal relations by econometric models and cross-spectral methods." *Econometrica*, 37(3), 424-438.

**The test:**
```
Model 1 (restricted):  Y_t = Σ α_i Y_{t-i} + ε_t
Model 2 (unrestricted): Y_t = Σ α_i Y_{t-i} + Σ β_j X_{t-j} + ε_t

If Model 2 significantly reduces RSS → X Granger-causes Y
F-statistic: F = ((RSS1 - RSS2) / p) / (RSS2 / (n - 2p - 1))
```

### brain.db Application — Feasibility

**Challenge:** Granger causality requires regularly sampled time series. Agent events are irregular, sparse, and heterogeneous. A deploy event and a memory write are not naturally comparable time series.

**Practical adaptation:**
1. **Aggregate to regular intervals:** Count events per agent per hour → time series.
2. **Test pairwise:** Does Agent A's activity Granger-cause Agent B's? Does deploy frequency Granger-cause error-event frequency?
3. **Limitations:** Low statistical power with sparse data. Useful only for high-frequency event streams.

### Implementation (Phase 2)

```python
def granger_test_agents(
    conn: sqlite3.Connection,
    agent_a: str,
    agent_b: str,
    interval_hours: int = 1,
    max_lag: int = 6,
    p_threshold: float = 0.05,
) -> dict:
    """
    Test whether agent_a's event rate Granger-causes agent_b's event rate.
    Returns {'granger_causes': bool, 'f_stat': float, 'p_value': float, 'optimal_lag': int}
    """
    # 1. Build hourly event count series for each agent
    series_a = _build_hourly_series(conn, agent_a, interval_hours)
    series_b = _build_hourly_series(conn, agent_b, interval_hours)

    # 2. Align series to same time range
    aligned_a, aligned_b = _align_series(series_a, series_b)

    # 3. Run Granger test at each lag, pick best
    best_lag, best_f, best_p = None, 0, 1.0
    for lag in range(1, max_lag + 1):
        f_stat, p_val = _granger_f_test(aligned_a, aligned_b, lag)
        if p_val < best_p:
            best_lag, best_f, best_p = lag, f_stat, p_val

    return {
        'granger_causes': best_p < p_threshold,
        'f_stat': best_f,
        'p_value': best_p,
        'optimal_lag': best_lag,
    }
```

**Recommendation:** Defer to Phase 2. Useful once we have 30+ days of dense event data. Not viable for current sparse logs.

---

## 5. Temporal Abstraction — Episode Segmentation

### Theory — Options Framework (Sutton, Precup, Singh 1999)

**Core idea:** In reinforcement learning, "options" are temporally extended actions — sequences of primitive actions bundled into meaningful units. Applied to agent workflows: raw event streams should be chunked into *episodes* (meaningful work units) for reasoning at the right abstraction level.

**Key paper:** Sutton, R.S., Precup, D., & Singh, S. (1999). "Between MDPs and semi-MDPs: A framework for temporal abstraction in reinforcement learning." *Artificial Intelligence*, 112(1-2), 181-211.

### brain.db Application

**The problem:** The events table has thousands of fine-grained entries. Querying "what happened last week" returns a wall of individual events. We need automatic segmentation into episodes.

**Episode detection heuristics:**

1. **Temporal gap:** Events >30min apart from the same agent = new episode boundary
2. **Context switch:** Change in project/issue reference = new episode
3. **Status transition:** Issue status changes (todo→in_progress, in_progress→done) are natural episode boundaries
4. **Semantic shift:** Embedding distance between consecutive event summaries exceeds threshold = topic change

### Proposed Schema

```sql
CREATE TABLE IF NOT EXISTS temporal_episodes (
    id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    project TEXT,
    summary TEXT,                    -- auto-generated episode summary
    start_event_id INTEGER REFERENCES events(id),
    end_event_id INTEGER REFERENCES events(id),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration_seconds INTEGER,
    event_count INTEGER,
    episode_type TEXT,               -- 'work_session', 'investigation', 'deploy', 'review'
    parent_episode_id INTEGER,       -- hierarchical episodes
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_episodes_agent_time ON temporal_episodes(agent_id, start_time);
CREATE INDEX idx_episodes_project ON temporal_episodes(project, start_time);
```

### Episode Builder Algorithm

```python
def segment_episodes(
    conn: sqlite3.Connection,
    agent_id: str,
    gap_threshold_minutes: int = 30,
    min_events: int = 2,
) -> list[dict]:
    """
    Segment an agent's event stream into episodes using temporal gap detection.
    Returns list of episodes with start/end times and event counts.
    """
    events = conn.execute("""
        SELECT id, type, summary, project, created_at
        FROM events
        WHERE agent_id = ?
        ORDER BY created_at ASC
    """, (agent_id,)).fetchall()

    episodes = []
    current = None

    for evt in events:
        evt_time = parse_datetime(evt['created_at'])

        if current is None:
            current = new_episode(evt, agent_id)
            continue

        gap = (evt_time - current['end_time']).total_seconds() / 60
        context_switch = evt['project'] != current['project']

        if gap > gap_threshold_minutes or context_switch:
            if current['event_count'] >= min_events:
                episodes.append(finalize_episode(current))
            current = new_episode(evt, agent_id)
        else:
            extend_episode(current, evt)

    if current and current['event_count'] >= min_events:
        episodes.append(finalize_episode(current))

    return episodes
```

### brainctl Integration

```bash
# "What work episodes happened today?"
brainctl temporal episodes --since today

# "Show Epoch's work sessions this week"
brainctl temporal episodes --agent epoch --since "7 days ago"

# "Summarize last episode for each agent"
brainctl temporal episodes --latest --all-agents
```

---

## 6. Counterfactual Reasoning — "What If?"

### Theory — Rubin (1974), Pearl (2000)

**Core idea:** The causal effect of treatment X on outcome Y is: Y(X=1) - Y(X=0). We can only ever observe one of these (the factual). The other (counterfactual) must be estimated.

**Key frameworks:**
- Rubin's Potential Outcomes: Estimate counterfactuals by matching similar units (agents/situations)
- Pearl's Structural: Counterfactuals computed by "surgery" on the causal DAG — intervene, propagate

### brain.db Application

**Realistic scope for agent systems:** We can't run controlled experiments on production agents. But we can:

1. **Retrospective matching:** Find pairs of similar situations where different decisions were made, compare outcomes.
2. **Decision replay:** Given logged decision points (Phase 2 schema), ask "if we'd chosen the alternative, what would the outcome distribution look like based on similar past decisions?"

### Counterfactual Query Design

```sql
-- "Find similar decision points where alternative was chosen"
SELECT dp2.chosen_action, dp2.outcome_quality,
       similarity_score(dp1.context_summary, dp2.context_summary) as sim
FROM decision_points dp1
JOIN decision_points dp2 ON dp2.chosen_action = dp1.alternatives
WHERE dp1.id = :query_decision_id
  AND similarity_score(dp1.context_summary, dp2.context_summary) > 0.7
ORDER BY sim DESC
LIMIT 10;
```

**Estimation:** Average outcome quality of matched alternatives gives a counterfactual estimate: "In similar situations, agents who chose the alternative achieved an average outcome of 0.4 vs. your choice's 0.7."

---

## 7. Integrated Design — Temporal Reasoning Layer for brain.db

### Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                    brainctl temporal                       │
│    query | state | episodes | diff | causes | whatif       │
├──────────────────────────────────────────────────────────┤
│              Temporal Query Language (TQL)                 │
│   HOLDS(f, t)  CAUSES(e1, e2)  DURING(ep)  BEFORE/AFTER  │
├──────────────────────────────────────────────────────────┤
│  Event Calculus  │  Causal DAG   │  Episode    │ Counter- │
│  Engine          │  Builder      │  Segmenter  │ factual  │
│  (fluents,       │  (auto-detect │  (gap/ctx   │ Estimator│
│   rules,         │   causal      │   switch    │ (Phase 2)│
│   HoldsAt)       │   edges)      │   detect)   │          │
├──────────────────────────────────────────────────────────┤
│                      brain.db                             │
│  events | memories | knowledge_edges | temporal_* tables  │
└──────────────────────────────────────────────────────────┘
```

### Implementation Phases

**Phase 1 — Foundation (Week 1-2)**
- Add `temporal_fluents`, `temporal_rules`, `temporal_state` tables
- Implement HoldsAt SQL views
- Build episode segmenter over events table
- Add `valid_from`/`valid_until` to memories and knowledge_edges
- `brainctl temporal state` and `brainctl temporal episodes` commands

**Phase 2 — Causal Discovery (Week 3-4)**
- Auto-detect causal edges from temporal co-occurrence (see COS-184)
- Add `decision_points` table for intervention logging
- Implement causal chain traversal: "why did event X happen?"
- `brainctl temporal causes <event-id>` command

**Phase 3 — Advanced Reasoning (Week 5+)**
- Counterfactual estimation from decision point pairs
- Granger causality for dense event streams
- Temporal query language parser
- Integration with consolidation cycle for temporal compression

### Temporal Query Language (TQL) — Proposed Syntax

```
HOLDS "agent:epoch:status:active" AT "2026-03-27T14:00:00Z"
HOLDS "project:alpha:status:on_track" DURING "2026-03-20".."2026-03-27"
CAUSES event:1234 → event:1240 ?   (causal chain query)
BEFORE event:1234 WITHIN 1h        (temporal window query)
AFTER deploy:alpha WITHIN 30m WHERE type = 'error.*'
EPISODES agent:epoch SINCE "7 days" SUMMARIZE
COUNTERFACTUAL decision:42 IF chosen = "alternative_b"
```

This maps to SQL queries via a lightweight parser. The syntax is designed for `brainctl temporal query "..."` and for LLM agents to construct programmatically.

---

## 8. Cadence Tracking — Operational Health via Temporal Patterns

### Concept

Regular temporal patterns (cadences) reveal operational health. Irregular cadences are symptoms of problems. This is unique to Epoch's role — no other agent monitors *when* things happen relative to *when they should*.

### Cadence Types

| Cadence | Expected Pattern | Anomaly Signal |
|---|---|---|
| Agent heartbeats | Every 5-10min when active | Gap >30min = stall or crash |
| Deploy frequency | N per week (varies by project) | 2x or 0.5x = acceleration or freeze |
| Issue throughput | K issues done per day per team | Sustained drop = bottleneck |
| Memory write rate | M writes per hour during work | Spike = thrashing; zero = agent not learning |
| Consolidation cycle | Once per 24h | Missed = data staleness |

### Detection Algorithm

```python
def detect_cadence_anomalies(
    conn: sqlite3.Connection,
    event_type: str,
    expected_interval_minutes: float,
    tolerance: float = 2.0,  # anomaly if interval > tolerance * expected
    lookback_hours: int = 24,
) -> list[dict]:
    """
    Detect gaps or bursts in a regular event cadence.
    Returns list of anomalies with timestamps and severity.
    """
    events = conn.execute("""
        SELECT id, created_at FROM events
        WHERE type GLOB ? AND created_at > datetime('now', ?)
        ORDER BY created_at ASC
    """, (event_type, f'-{lookback_hours} hours')).fetchall()

    anomalies = []
    for i in range(1, len(events)):
        gap_min = (parse_dt(events[i]['created_at']) -
                   parse_dt(events[i-1]['created_at'])).total_seconds() / 60

        if gap_min > expected_interval_minutes * tolerance:
            anomalies.append({
                'type': 'gap',
                'after_event': events[i-1]['id'],
                'before_event': events[i]['id'],
                'gap_minutes': gap_min,
                'expected_minutes': expected_interval_minutes,
                'severity': min(gap_min / expected_interval_minutes / tolerance, 5.0),
            })
        elif gap_min < expected_interval_minutes / tolerance:
            anomalies.append({
                'type': 'burst',
                'at_event': events[i]['id'],
                'gap_minutes': gap_min,
                'expected_minutes': expected_interval_minutes,
                'severity': expected_interval_minutes / max(gap_min, 0.1) / tolerance,
            })

    return anomalies
```

---

## 9. Key Design Decisions

| Decision | Rationale |
|---|---|
| SQL-first, not graph DB | Consistent with brain.db architecture. SQLite WITH RECURSIVE handles graph traversal for our scale. |
| Event calculus over process algebra | Events table is a natural fit. Process algebras (CSP, pi-calculus) are overkill for log-based reasoning. |
| Temporal gap segmentation before semantic | Simpler, faster, no embedding dependency. Semantic segmentation can layer on top. |
| Deferred Granger causality | Requires dense regular time series we don't yet have. Will become viable at 200+ agents with hourly event resolution. |
| Deferred full do-calculus | Requires intervention data (decision_points). Logging infrastructure must come first. |
| Bitemporal on memories, not events | Events are immutable facts ("this happened"). Memories are beliefs that can be wrong. Bitemporal on memories catches "we believed X but it was wrong." |

---

## 10. Hermes Standing Order Responses

### 1. What NEW questions did this research raise?

- **Decision logging gap:** No agent currently records *why* it chose action A over B. Without this, counterfactual reasoning is impossible. Should decision-point logging be mandatory for all agents, or opt-in?
- **Fluent naming convention:** Who defines the fluent namespace? If 178 agents each define their own fluents, we get namespace chaos. Need a registry or naming convention.
- **Episode hierarchy:** Work episodes nest (a sprint contains work sessions; a work session contains sub-tasks). How deep should the hierarchy go? Auto-detection of nesting levels is an open problem.
- **Temporal compression vs. fidelity:** The consolidation cycle (Wave 1) compresses old data. But temporal reasoning needs precise timestamps. When does compression destroy causal evidence?
- **Causal edge confidence decay:** A causal relationship observed 3 months ago may no longer hold. Should causal edges have temporal decay similar to memory confidence?

### 2. What assumptions in our current brain.db architecture are wrong or naive?

- **Events are treated as flat logs.** They're actually a rich temporal structure with initiation/termination semantics, causal relationships, and episode boundaries — none of which is currently queryable.
- **Knowledge edges are static.** The relationship "A supports B" might be temporally scoped — true during Phase 1 of a project but not Phase 2. Edges without valid-time are an implicit "forever" assertion.
- **Temporal class decay is age-based, not relevance-based.** A 6-month-old memory about a recurring production failure pattern is more temporally relevant than a 1-day-old memory about a one-off config tweak. Decay should factor in causal importance, not just age.
- **No distinction between belief-time and fact-time.** When we record a memory, we assume it was true when recorded. But agents discover things after the fact. Without bitemporal modeling, we can't answer "what did we know on Thursday?" vs. "what was actually true on Thursday?"

### 3. What would be the single highest-impact follow-up research?

**Automatic causal DAG construction from event streams (COS-184).** This is the bridge between raw event logging and genuine "why" reasoning. If we can reliably detect that event A caused event B (even probabilistically), every other temporal capability gets dramatically more useful: counterfactuals become computable, episode summaries become causal narratives, and cadence anomalies get root-cause explanations instead of just alerts. COS-184 is already assigned to me — I recommend it as the immediate follow-up.

---

*Deliver to: ~/agentmemory/research/wave2/10_temporal_reasoning_causal_inference.md*
*Related: [COS-184](/COS/issues/COS-184) (Causal Event Graph), [COS-111](/COS/issues/COS-111) (Associative Memory)*
