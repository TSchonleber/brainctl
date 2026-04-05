# COS-204: Memory as a Policy Engine — Distributed Decisions Without a Central Oracle

**Series:** Cognitive Operating System Research
**Wave:** 5
**Report Number:** 15
**Date:** 2026-03-28
**Author:** Claude Code (claude-sonnet-4-6), on behalf of Hermes
**Status:** Draft — ready for review

---

## Abstract

Hermes currently routes all significant decisions through a central orchestration layer, creating a bottleneck that constrains throughput, introduces latency, and produces a single point of failure. This report proposes an alternative: a **memory-driven policy engine** in which individual agents query accumulated organizational memory to make locally-correct decisions without escalating every case to the oracle.

The central claim is that many decision classes that appear to require central judgment are actually retrievable from prior experience encoded in memory. When an agent asks "should I use approach A or B?", the relevant signal already exists in `brain.db` as a pattern of outcomes across prior tasks. Formalizing that signal as a **policy memory** — a retrievable, versioned, context-sensitive directive — allows agents to act on organizational wisdom without interrupting the orchestrator.

This report defines what constitutes a safe policy delegation, distinguishes memory-driven policy from hard-coded rules, catalogs failure modes, proposes a feedback loop for policy freshness, relates the architecture to COS-180's goal-feedback work, and specifies a concrete schema and query interface directly implementable in brain.db.

---

## Table of Contents

1. [Research Questions and Scope](#1-research-questions-and-scope)
2. [Decision Classes Safe for Memory-Driven Policy Delegation](#2-decision-classes-safe-for-memory-driven-policy-delegation)
3. [Memory-Policy vs. Hard-Coded Rule: What Makes It Adaptive?](#3-memory-policy-vs-hard-coded-rule-what-makes-it-adaptive)
4. [Failure Modes](#4-failure-modes)
5. [Feedback Loop: Keeping Policies Current](#5-feedback-loop-keeping-policies-current)
6. [Relationship to COS-180: Goal Proposals and Policy Orthogonality](#6-relationship-to-cos-180-goal-proposals-and-policy-orthogonality)
7. [Concrete Architecture](#7-concrete-architecture)
8. [New Questions Raised by This Research](#8-new-questions-raised-by-this-research)
9. [Assumptions That Are Wrong or Naive](#9-assumptions-that-are-wrong-or-naive)
10. [Highest-Impact Follow-Up Research](#10-highest-impact-follow-up-research)
11. [References](#11-references)

---

## 1. Research Questions and Scope

This report addresses five primary research questions:

1. What decision classes are safe to delegate to memory-driven policy?
2. How is a memory-policy distinguished from a hard-coded rule, and what makes it adaptive?
3. What failure modes exist — policy capture, stale policy, conflicting policies?
4. What feedback loop keeps policies current as organizational context changes?
5. What is the relationship to COS-180 (memory-to-goal feedback) — do goal proposals become policies, or are they orthogonal?

**Scope boundaries:** This report addresses decision delegation within the Hermes multi-agent system (178 agents, brain.db SQLite backend). It does not address inter-system policy federation, user-facing policy disclosure, or regulatory compliance requirements. Recommendations are intended to be directly implementable against the existing `brain.db` schema without requiring a full architectural rebuild.

---

## 2. Decision Classes Safe for Memory-Driven Policy Delegation

### 2.1 The Delegation Safety Spectrum

Not all decisions are equally safe to delegate to a memory-driven policy engine. Safety depends on two independent axes:

- **Reversibility:** Can the action be undone or corrected within the task lifecycle, or is it terminal (e.g., sending an external communication, deleting data)?
- **Contextual stability:** Does the decision class depend primarily on slow-moving organizational context (preference, style, routing conventions) or on fast-moving situational context (live system state, user emotional state, novel edge cases)?

A decision is a safe delegation candidate when it is high-reversibility **or** high contextual stability (ideally both). A decision is unsafe for delegation when it is both low-reversibility and high contextual instability.

```
                     CONTEXTUAL STABILITY
                   Low               High
                ┌──────────────┬────────────────┐
   High         │  CONDITIONAL │   SAFE TO      │
Reversibility   │  (escalate   │   DELEGATE     │
                │  on novelty) │                │
                ├──────────────┼────────────────┤
   Low          │  MUST        │   SAFE WITH    │
Reversibility   │  ESCALATE    │   STALENESS    │
                │              │   GUARD        │
                └──────────────┴────────────────┘
```

### 2.2 Safe Decision Classes

The following classes fall in the safe-to-delegate region for Hermes:

**Task Routing**
Which agent or agent-class handles a given task type is primarily a function of historical performance, specialization, and load patterns — all encodable in memory. Example policy: *"Tasks of type `data_analysis` with dataset size > 10k rows are routed to agents with the `pandas-specialist` tag unless queue depth exceeds 5."* This is high-reversibility (reassignment is cheap) and high contextual stability (agent capabilities change slowly).

**Escalation Thresholds**
When to escalate vs. handle locally depends on accumulated experience with task complexity distributions. A policy memory encoding "tasks that match pattern X have historically required human review 80% of the time" allows agents to self-escalate without asking Hermes first.

**Communication Tone and Register**
Tone adaptation (formal vs. casual, verbose vs. terse) depends on recipient identity, thread history, and organizational culture — all stable, memory-derivable signals. An agent querying "what communication register does recipient R prefer?" can retrieve this from prior interaction memories.

**Retry and Backoff Strategies**
How many times to retry a failing subtask before escalating, and with what backoff, is learnable from outcomes. Memory encodes which retry patterns resolved similar failures. This is safe because retries are fully reversible.

**Output Format Selection**
Choosing JSON vs. Markdown vs. plain text, verbosity level, and whether to include citations or confidence scores depends on consumer identity and task type. Stable, memory-derivable.

**Deadlock Detection Heuristics**
Patterns that have historically preceded deadlocks (e.g., mutual wait on resource types A and B) can be encoded as policy: "if conditions X and Y are both present, break the dependency chain by yielding resource A first."

**Caching and Memoization Decisions**
Whether to compute fresh or serve cached results depends on result volatility and consumer tolerance for staleness — both patterns that emerge from memory.

### 2.3 Conditional Delegation Classes

These classes can be delegated but require a staleness guard or novelty detector:

- **Security posture decisions** (safe if policy is fresh; dangerous if stale during an active threat)
- **Resource allocation** (safe in normal operating conditions; unsafe during capacity crises)
- **Agent trust decisions** (safe when the trust model is stable; unsafe when a new agent type is introduced)

### 2.4 Classes That Must Escalate

- **Novel task types** with no memory coverage (no prior outcomes to inform a policy)
- **Irreversible external actions** (sending official communications, financial transactions, permanent deletions)
- **User-explicit override requests** (user directly contradicts the standing policy)
- **Policy contradictions** that the conflict resolver cannot resolve without human judgment

---

## 3. Memory-Policy vs. Hard-Coded Rule: What Makes It Adaptive?

### 3.1 The Hard-Coded Rule Trap

Hard-coded rules are **brittle organizational knowledge**. They encode what was true at authoring time. The Hermes codebase almost certainly contains rules like:

```python
if task.type == "report_generation" and task.complexity > 7:
    escalate_to_hermes()
```

This rule cannot update when:
- The complexity scale is recalibrated
- A new agent class makes complex reports routine
- Organizational priorities shift such that complex reports should be handled faster, not escalated

Hard-coded rules fail silently — they continue to execute long after the conditions that justified them have changed.

### 3.2 What Distinguishes a Memory-Policy

A memory-policy is distinguished by three properties:

**1. Empirical provenance.** The policy was derived from observed outcomes, not authored by hand. The policy record carries a lineage: which experiences generated it, what outcome distribution supported it, and what statistical confidence backs it.

**2. Versioned and time-stamped.** Every policy memory has a `created_at`, `last_validated_at`, and an `expires_at` or `staleness_ttl`. It is not immortal.

**3. Context-sensitive retrieval.** Policies are not looked up by key — they are retrieved by semantic similarity to the current decision context. An agent querying "routing decision for large text summarization task" may retrieve a policy authored for "report generation with high word count" if the embedding distance is within threshold. This means policies generalize without explicit enumeration.

### 3.3 Adaptivity Mechanisms

A memory-policy adapts through three channels:

**Reinforcement from outcomes.** Every time a policy is invoked, the resulting outcome is eventually written back to memory. If outcomes degrade, the policy's confidence score drops. Below a threshold, the policy is flagged for review or reverted.

**Supersession.** A more recent policy with higher confidence and overlapping scope supersedes an older one. The old policy is archived, not deleted — this preserves audit history and allows rollback.

**Contextual drift detection.** If the distribution of incoming decision contexts shifts significantly (measured by embedding distance from the policy's training context), the policy is flagged as potentially out-of-distribution before outcomes can confirm the problem.

### 3.4 The Spectrum from Rule to Policy to Learning

```
Hard-coded rule → Memory-policy → Reinforcement-learned policy
     (static)         (empirical,          (continuously updated
                       versioned)            from live feedback)
```

The architecture proposed in Section 7 implements the middle tier — memory-policy — which is achievable with brain.db without requiring a full RL training loop. The third tier (RL-learned policy) is a future-state architecture addressed in Section 10.

---

## 4. Failure Modes

### 4.1 Policy Capture

**Definition:** A policy is captured when it consistently reflects the preferences of a dominant agent or user at the expense of broader organizational correctness.

**Mechanism:** If a high-volume agent generates a disproportionate share of the outcome signals that reinforce a policy, the policy drifts toward that agent's behavior patterns. Other agents using the policy then receive guidance optimized for a context different from their own.

**Detection signal:** Policy utilization distribution becomes skewed — most invocations come from one agent or agent class. Outcome quality variance increases for under-represented invokers.

**Mitigation:**
- Weight outcome signals by invoker diversity, not just frequency
- Flag policies where the top-3 invokers account for > 60% of reinforcement events
- Require cross-agent validation before promoting a policy to `canonical` status

### 4.2 Stale Policy

**Definition:** A policy that was accurate at creation time but no longer reflects organizational reality due to context drift.

**Mechanism:** The policy was derived from a period when, for example, Agent Class A was reliable for task type X. That class was subsequently deprecated or significantly modified, but the policy continues to route tasks to it.

**Detection signal:**
- Time-based: `last_validated_at` exceeds `staleness_ttl`
- Outcome-based: rolling outcome quality for this policy has degraded by > N% from baseline
- Context-based: embedding distance between current invocation contexts and the policy's `training_context_centroid` exceeds threshold

**Mitigation:**
- Hard expiry via `expires_at` (force re-derivation)
- Soft expiry via staleness confidence decay (policy confidence decreases linearly after `last_validated_at + half_life`)
- Mandatory re-validation triggered by org-level change events (agent additions/removals, schema migrations)

### 4.3 Conflicting Policies

**Definition:** Two or more active policies give contradictory guidance for the same decision context.

**Mechanism:** Policy A was derived from Task Context 1 and Policy B from Task Context 2. A new task arrives with context that falls within the overlap region of both policies' retrieval scopes.

**This is the most dangerous failure mode** because it can produce non-deterministic agent behavior — different agents or the same agent at different times may retrieve different policies and act differently on equivalent tasks.

**Detection signal:** A retrieval query returns two or more policies with cosine similarity > 0.85 to each other's `context_embedding` but with divergent `directive` fields.

**Mitigation:** See Section 7.5 (Conflict Resolution Architecture).

### 4.4 Policy Laundering

**Definition:** A bad outcome is repeated multiple times, and each repetition reinforces a policy that produces more bad outcomes.

**Mechanism:** An agent uses a flawed approach, the outcome is rated "acceptable" (perhaps by a lenient evaluator or automated check), and the approach is encoded as a policy. Subsequent agents follow the policy, producing similarly flawed outcomes, which further reinforce the policy.

**Detection signal:** Policy-derived decisions cluster in outcome quality at the lower end of acceptable — many "acceptable" but few "excellent" outcomes. No A/B comparison exists against the counterfactual (not using the policy).

**Mitigation:**
- Periodic A/B shadow testing: for a sample of invocations, both the policy recommendation and an independent agent judgment are recorded. Compare outcomes.
- Require outcome ratings from multiple independent evaluators before reinforcing high-stakes policies.

### 4.5 Policy Explosion

**Definition:** The `policies` table grows unbounded as new policies are created for every decision variant, making retrieval noisy and computationally expensive.

**Mechanism:** The policy creation mechanism is too sensitive. Minor context variations trigger creation of new policies rather than updating existing ones.

**Mitigation:**
- Merge candidate detection: before creating a new policy, search for existing policies with embedding distance < 0.15. If found, update rather than create.
- Entropy budget: cap the number of active policies per decision category. Promotion to active requires retiring or merging an existing policy.

---

## 5. Feedback Loop: Keeping Policies Current

### 5.1 The Policy Lifecycle

```
  [Experience accumulates in memory]
           │
           ▼
  [Policy derivation: pattern extraction
   from outcome-tagged memories]
           │
           ▼
  [Policy validation: cross-agent review,
   statistical confidence check]
           │
           ▼
  [Policy activation: status = 'active',
   available for retrieval]
           │
           ▼
  [Policy invocation: agents query and act]
           │
           ▼
  [Outcome recording: task result tagged
   with policy_id that informed decision]
           │
           ▼
  [Policy health monitoring: rolling
   outcome quality, staleness tracking]
           │
     ┌─────┴──────┐
     │            │
  [Healthy:    [Degraded:
   continue]    flag for review → re-derivation
                                 or deprecation]
```

### 5.2 Outcome Attribution

For feedback to work, the system must track which policy (if any) informed a given decision and link it to the ultimate task outcome. This requires:

1. **Decision logging:** When an agent queries a policy and acts on it, log the `policy_id`, `agent_id`, `task_id`, and the specific `directive` returned.
2. **Outcome tagging:** When a task completes, its outcome record includes a `policy_ids_invoked` list.
3. **Attribution rollup:** A background process periodically aggregates outcomes by `policy_id`, computing rolling success rate, quality distribution, and trend direction.

### 5.3 Org-Level Change Events as Policy Invalidation Triggers

Beyond time-based staleness, certain organizational events should trigger immediate policy review:

| Event | Policies Affected |
|---|---|
| New agent type added | Policies governing task routing, agent selection |
| Agent type deprecated | Any policy that references that agent type in its directive |
| Schema migration in brain.db | Policies derived from data in changed tables |
| User preference override recorded | Policies governing communication style for that user |
| Significant failure event (incident) | All policies active during the incident window |
| New organizational objective set | Policies governing priority ordering and escalation |

These events should be publishable to a `policy_invalidation_events` table that the policy health monitor subscribes to.

### 5.4 The "Wisdom Half-Life" Concept

Different decision classes have different rates of organizational change. A policy governing communication tone with a specific user may be stable for months. A policy governing which infrastructure agent to use for deployments may be stale within weeks.

Each policy category should have a configurable `wisdom_half_life` — the time after which confidence decays to 50% of its original value in the absence of fresh reinforcement. This makes staleness smooth rather than binary.

```
confidence_effective = confidence_at_creation * (0.5 ^ (age_days / half_life_days))
```

A policy with confidence below `min_confidence_threshold` (e.g., 0.4) should not be returned as the primary recommendation. It may still be returned as a "historical note" alongside a recommendation to escalate.

---

## 6. Relationship to COS-180: Goal Proposals and Policy Orthogonality

### 6.1 Summary of COS-180's Architecture

COS-180 (Memory-to-Goal Feedback) addresses how accumulated memory informs goal proposals — the mechanism by which Hermes or its agents identify objectives worth pursuing based on patterns in historical outcomes. The core loop is: memory → pattern recognition → goal proposal → evaluation → adoption or rejection.

### 6.2 The Surface-Level Confusion

Goal proposals and policies appear similar at first glance: both are derived from memory, both influence agent behavior, both are versioned and time-sensitive. The temptation is to treat them as the same construct.

**This temptation should be resisted.** Conflating goals and policies produces a system where agents are unclear whether they are being told *what to aim for* or *how to act*. This ambiguity is dangerous.

### 6.3 The Correct Distinction

| Dimension | Goal (COS-180) | Policy (This report) |
|---|---|---|
| **Nature** | Desired future state | Decision heuristic for current action |
| **Timeframe** | Medium-to-long horizon | Immediate action guidance |
| **Subject** | What the system should achieve | How an agent should behave |
| **Adoption** | Requires evaluation and adoption decision | Activated after validation, then queried |
| **Override** | Requires goal revision process | Can be superseded by higher-priority policy or escalation |
| **Feedback** | Did we achieve the goal? | Did this decision produce a good outcome? |

### 6.4 How They Interact

Goals and policies are **not orthogonal** — they interact through two channels:

**Goals constrain policy scope.** An active goal (e.g., "reduce task latency by 30%") should influence which policies are prioritized during retrieval. An agent making a routing decision while a latency-reduction goal is active should weight policies that have historically produced faster completions.

**Goal achievement evidence can trigger policy revision.** If the system is consistently failing to achieve a goal, the policies that governed the relevant decision classes during that period should be reviewed. Persistent goal failure is a signal of policy inadequacy.

**But goal proposals do not become policies.** A goal proposal is a statement about desired outcomes. A policy is a decision heuristic. Converting one to the other requires an explicit derivation step: "given that we want to achieve goal G, what decision patterns should agents follow?" This derivation is non-trivial and should be a deliberate process, not automatic.

### 6.5 Recommendation

Maintain separate tables (`goal_memories` per COS-180, `policies` per this report). Add a foreign key `derived_from_goal_id` in the `policies` table to allow tracing when a policy was explicitly derived to serve a goal. Add a `goal_context` field in policy queries so that goal-aware retrieval can filter and rank policies.

---

## 7. Concrete Architecture

### 7.1 Schema: The `policies` Table

The following schema is designed for SQLite (brain.db) compatibility. It avoids JSON columns except for fields requiring flexible structure, using SQLite's JSON functions for querying.

```sql
CREATE TABLE policies (
    id                        TEXT PRIMARY KEY,          -- UUID
    name                      TEXT NOT NULL,             -- human-readable identifier
    category                  TEXT NOT NULL,             -- 'routing', 'escalation', 'tone',
                                                         -- 'retry', 'format', 'caching', etc.
    status                    TEXT NOT NULL DEFAULT 'candidate',
                                                         -- 'candidate' | 'active' | 'deprecated' | 'archived'
    priority                  INTEGER NOT NULL DEFAULT 50, -- 0-100; higher = higher precedence
    scope_agent_types         TEXT,                      -- JSON array of agent type tags, or NULL for all
    scope_task_types          TEXT,                      -- JSON array of task type strings, or NULL for all

    -- The directive itself
    directive                 TEXT NOT NULL,             -- natural language or structured directive
    directive_format          TEXT NOT NULL DEFAULT 'natural_language',
                                                         -- 'natural_language' | 'structured_json' | 'rule_expression'
    directive_json            TEXT,                      -- structured version if directive_format != nl

    -- Semantic retrieval
    context_description       TEXT NOT NULL,             -- the decision context this policy applies to
    context_embedding         BLOB,                      -- float32[] vector embedding of context_description
    context_keywords          TEXT,                      -- JSON array of keywords for hybrid retrieval

    -- Provenance
    derived_from_memory_ids   TEXT,                      -- JSON array of memory IDs that generated this policy
    derived_from_goal_id      TEXT REFERENCES goals(id), -- if derived to serve a COS-180 goal
    authored_by               TEXT NOT NULL,             -- agent_id or 'hermes' or 'user'
    derivation_method         TEXT,                      -- 'manual' | 'outcome_aggregation' | 'llm_synthesis'

    -- Confidence and staleness
    confidence                REAL NOT NULL DEFAULT 0.5, -- 0.0-1.0
    confidence_min_threshold  REAL NOT NULL DEFAULT 0.4, -- below this, policy is advisory only
    wisdom_half_life_days     INTEGER NOT NULL DEFAULT 30,
    created_at                TEXT NOT NULL DEFAULT (datetime('now')),
    last_validated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    last_invoked_at           TEXT,
    expires_at                TEXT,                      -- hard expiry; NULL = no hard expiry

    -- Outcome tracking
    invocation_count          INTEGER NOT NULL DEFAULT 0,
    successful_outcome_count  INTEGER NOT NULL DEFAULT 0,
    failed_outcome_count      INTEGER NOT NULL DEFAULT 0,
    outcome_quality_sum       REAL NOT NULL DEFAULT 0.0, -- sum of 0.0-1.0 quality scores

    -- Conflict tracking
    superseded_by_policy_id   TEXT REFERENCES policies(id),
    conflicts_with_policy_ids TEXT,                      -- JSON array of policy IDs with known conflicts
    conflict_resolution_rule  TEXT,                      -- 'use_higher_priority' | 'use_more_recent' |
                                                         -- 'use_higher_confidence' | 'escalate'

    -- Audit
    version                   INTEGER NOT NULL DEFAULT 1,
    change_log                TEXT,                      -- JSON array of change event objects

    FOREIGN KEY (superseded_by_policy_id) REFERENCES policies(id)
);

-- Indexes for common retrieval patterns
CREATE INDEX idx_policies_status_category ON policies(status, category);
CREATE INDEX idx_policies_last_validated ON policies(last_validated_at);
CREATE INDEX idx_policies_confidence ON policies(confidence);
CREATE INDEX idx_policies_category_priority ON policies(category, priority DESC);
```

### 7.2 Schema: `policy_invocations` Table

```sql
CREATE TABLE policy_invocations (
    id                TEXT PRIMARY KEY,
    policy_id         TEXT NOT NULL REFERENCES policies(id),
    agent_id          TEXT NOT NULL,
    task_id           TEXT,
    session_id        TEXT,

    -- Decision context at invocation time
    query_context     TEXT NOT NULL,             -- the context string the agent provided
    retrieval_score   REAL,                      -- cosine similarity or BM25 score
    directive_returned TEXT NOT NULL,            -- the actual directive text returned

    -- Outcome (filled in after task completion)
    outcome_quality   REAL,                      -- 0.0-1.0, filled after task completion
    outcome_category  TEXT,                      -- 'success' | 'partial' | 'failure' | 'escalated'
    outcome_notes     TEXT,

    invoked_at        TEXT NOT NULL DEFAULT (datetime('now')),
    outcome_recorded_at TEXT
);

CREATE INDEX idx_invocations_policy_id ON policy_invocations(policy_id);
CREATE INDEX idx_invocations_agent_id ON policy_invocations(agent_id);
CREATE INDEX idx_invocations_task_id ON policy_invocations(task_id);
CREATE INDEX idx_invocations_outcome_quality ON policy_invocations(outcome_quality);
```

### 7.3 Schema: `policy_invalidation_events` Table

```sql
CREATE TABLE policy_invalidation_events (
    id                TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,   -- 'agent_added' | 'agent_deprecated' | 'schema_migration' |
                                       -- 'preference_override' | 'incident' | 'goal_change'
    event_description TEXT NOT NULL,
    affected_categories TEXT,          -- JSON array of policy categories to review, or NULL for all
    affected_policy_ids TEXT,          -- JSON array of specific policy IDs, or NULL for category-wide
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at      TEXT,            -- NULL until the health monitor has processed this event
    processing_notes  TEXT
);
```

### 7.4 Agent Query Interface

Agents query the policy engine via `brainctl`. The interface is designed to be simple enough for any agent to use without deep knowledge of the underlying schema.

**Standard policy query:**
```bash
brainctl memory search \
  --category policy \
  --context "routing decision for large text summarization task with 50k tokens" \
  --agent-type "text-processor" \
  --task-type "summarization" \
  --min-confidence 0.5 \
  --max-results 3 \
  --format json
```

**Example response structure:**
```json
{
  "policies": [
    {
      "id": "pol_7a8f3c...",
      "name": "large-summarization-routing-v2",
      "category": "routing",
      "directive": "Route summarization tasks exceeding 30k tokens to agents tagged 'long-context-specialist'. If no such agent is available with queue depth < 3, split the task into 25k-token chunks and route each chunk to standard summarization agents.",
      "confidence": 0.78,
      "confidence_effective": 0.71,
      "retrieval_score": 0.89,
      "last_validated_at": "2026-03-01T14:23:00Z",
      "staleness_warning": false,
      "invocation_count": 143,
      "success_rate": 0.82
    }
  ],
  "escalation_recommended": false,
  "conflict_detected": false
}
```

**Staleness-aware query (returns confidence-decayed results with warnings):**
```bash
brainctl memory search \
  --category policy \
  --context "escalation threshold for data validation tasks" \
  --staleness-mode warn    # options: ignore | warn | block
```

**Checking for policy conflicts before acting:**
```bash
brainctl policy check-conflicts \
  --policy-id "pol_7a8f3c..." \
  --context "routing decision for large text summarization"
```

**Recording an outcome against a policy invocation:**
```bash
brainctl policy record-outcome \
  --invocation-id "inv_9b2d1e..." \
  --quality 0.85 \
  --category success \
  --notes "Task completed in 4.2s, within latency budget"
```

### 7.5 Staleness Guards

Staleness is enforced at query time by computing `confidence_effective`:

```python
def compute_effective_confidence(policy: Policy, now: datetime) -> float:
    age_days = (now - policy.last_validated_at).days
    decay = 0.5 ** (age_days / policy.wisdom_half_life_days)
    confidence_effective = policy.confidence * decay
    return confidence_effective

def query_policies(context: str, min_confidence: float = 0.5) -> list[PolicyResult]:
    candidates = retrieve_by_embedding(context)
    results = []
    for policy in candidates:
        effective_conf = compute_effective_confidence(policy, datetime.now())
        if effective_conf < policy.confidence_min_threshold:
            # Policy too stale to recommend; log but do not return as primary
            log_staleness_event(policy)
            continue
        staleness_warning = effective_conf < min_confidence
        results.append(PolicyResult(
            policy=policy,
            confidence_effective=effective_conf,
            staleness_warning=staleness_warning,
            retrieval_score=cosine_similarity(context, policy.context_embedding)
        ))
    return sorted(results, key=lambda r: r.retrieval_score * r.confidence_effective, reverse=True)
```

Hard expiry is enforced by filtering `WHERE expires_at IS NULL OR expires_at > datetime('now')` in the base query.

### 7.6 Conflict Resolution

**Detection:** Two policies conflict if they are both `active`, share a category, have overlapping `scope_agent_types` and `scope_task_types`, and their `directive` embeddings are in a region that would produce incompatible actions (detected by a conflict classifier or heuristic divergence check).

**Resolution hierarchy:**

1. **Explicit priority:** If one policy has a higher `priority` score, it wins. The lower-priority policy is returned as a secondary note.

2. **Recency:** If priorities are equal, the more recently validated policy wins.

3. **Confidence:** If recency is within 24 hours, higher `confidence_effective` wins.

4. **Scope specificity:** A policy with a narrower scope (more specific `scope_agent_types` or `scope_task_types`) wins over a broader policy for the specific context.

5. **Escalation:** If none of the above resolves the conflict unambiguously (e.g., priorities equal, same validation date, same confidence), return both policies to the agent with `conflict_detected: true` and `escalation_recommended: true`. Log a `policy_invalidation_event` for human review.

**Schema support for known conflicts:**

When a conflict is detected and resolved, it is recorded:

```sql
UPDATE policies
SET conflicts_with_policy_ids = json_insert(
    COALESCE(conflicts_with_policy_ids, '[]'),
    '$[#]',
    'pol_conflicting_id'
),
conflict_resolution_rule = 'use_higher_priority'
WHERE id = 'pol_winning_id';
```

This ensures that subsequent retrievals in the same conflict region immediately return the resolution without re-running the conflict detection logic.

### 7.7 Policy Derivation Workflow

Policies are not authored manually in the steady state. They are derived from memory:

```
1. Identify high-frequency decision context (query analysis)
2. Retrieve all memories with outcome tags in that context
3. Cluster outcomes by decision type (approach A vs B vs C)
4. Compute outcome quality distribution per cluster
5. If one cluster dominates (> 2x quality score, > 20 samples), derive candidate policy
6. Submit candidate for cross-agent validation (sample 3 agents, run shadow comparison)
7. If validation passes (>= 2/3 agents improve outcomes), promote to 'active'
8. Record derivation provenance in derived_from_memory_ids
```

This workflow can be initiated by Hermes periodically or triggered by agents that notice they are escalating a similar decision repeatedly.

---

## 8. New Questions Raised by This Research

1. **Policy attribution in multi-step tasks.** When a task involves five sequential agent decisions, each potentially informed by a different policy, how is outcome quality distributed across all contributing policies? Simple last-policy-wins attribution will systematically over-credit or under-credit upstream decisions.

2. **Policy personalization vs. organizational canon.** Should individual agents be allowed to develop agent-specific policy overrides, or must all policies be org-wide? A personalized policy may be more accurate for a specific agent's context but degrades the org's ability to reason about consistent behavior.

3. **The cold-start problem.** For new decision categories with no memory coverage, there are no policies and no outcomes to derive them from. What is the mechanism for seeding initial policies (manual authoring? conservative defaults?) and how long must the system accumulate evidence before auto-derived policies are trusted?

4. **Policy legibility.** If a policy is derived from 500 memory records via embedding aggregation, can a human or auditing agent understand *why* the policy says what it says? Explainability of derived policies may be legally or operationally required in some contexts.

5. **Adversarial policy injection.** Could a compromised agent deliberately produce outcomes designed to reinforce a policy beneficial to the attacker? What are the constraints needed to prevent memory poisoning via policy reinforcement?

6. **Policy scope for inter-agent communication.** This report focuses on single-agent decisions. But policies governing how agents communicate with *each other* — message formats, trust levels, priority signals — may require a different retrieval model than policies governing task execution.

7. **Policy freshness vs. policy stability tradeoff.** Rapid policy turnover in response to recent outcomes produces high adaptivity but low stability. Agents cannot rely on consistent behavior if policies update frequently. What is the right temporal granularity for policy updates?

8. **The observer effect.** Does the act of retrieving a policy change agent behavior in ways that make the policy self-reinforcing regardless of its quality? If all agents follow the policy, there is no counterfactual to measure improvement or degradation against.

---

## 9. Assumptions That Are Wrong or Naive

**Assumption 1: "Policies can be retrieved by semantic search on context."**
This assumes that the current decision context can be accurately embedded and that context embeddings are stable over time. In practice, the same semantic content may be described differently by different agents, and embedding models may drift if updated. Without embedding model versioning and a re-embedding strategy, policies authored under one model may not be retrievable under a newer one.

**Assumption 2: "Outcome quality can be measured and attributed accurately."**
Many task outcomes are ambiguous, delayed, or multi-causal. "The report was good" is a judgment that depends on who is judging, when, and against what baseline. Assuming a clean quality signal of 0.0-1.0 is available per task is optimistic. Without investment in outcome measurement infrastructure, the feedback loop will be noisy to the point of dysfunction.

**Assumption 3: "Policy conflicts are rare and detectable."**
Conflicts may be far more common than anticipated, especially as the policy set grows. The conflict detection mechanism described here relies on embedding distance between directives — but two policies can give logically contradictory guidance while being semantically similar (e.g., both use the word "prioritize" but about different things). Syntactic conflict detection is insufficient; semantic conflict detection requires richer representation.

**Assumption 4: "178 agents represent a stable system."**
The architecture assumes agent count and agent capabilities are relatively stable. In practice, Hermes likely adds and removes agents frequently. Each change should trigger a policy review sweep, but if agent churn is high, the review queue will be perpetually backlogged, and staleness events will go unprocessed.

**Assumption 5: "Agents will faithfully record their policy invocations and outcomes."**
The feedback loop depends on agents writing back to `policy_invocations` after task completion. If agents are not disciplined about this (or if the write path fails silently), policies will be invoked in a dark room — no signal returns. This requires instrumentation investment before the feedback loop works.

**Assumption 6: "A single `policies` table is sufficient for all decision classes."**
Different decision classes may warrant fundamentally different policy representations. Communication tone policies look very different from routing policies — the former is essentially a style guide, the latter a conditional routing tree. A flat table with a flexible `directive` text field will work initially but may become an impediment as the system matures and different classes require different query patterns.

**Assumption 7: "Policies improve with more data."**
More outcome data improves policy confidence only if the data is representative and the measurement is accurate. In biased systems (see policy capture, Section 4.1), more data makes a bad policy more entrenched, not better. The architecture must include diversity guarantees in the training data for policy derivation.

---

## 10. Highest-Impact Follow-Up Research

**Priority 1: Outcome Measurement Infrastructure (COS-205)**
The feedback loop is the entire point of a memory-policy system, and it depends on accurate, timely outcome measurement. COS-205 should define: what counts as a task outcome, who evaluates quality, what latency is acceptable between task completion and outcome recording, and how multi-step task outcomes are decomposed. Without this, the policy engine is write-only.

**Priority 2: Embedding Stability and Model Versioning (COS-206)**
Semantic retrieval depends on stable embeddings. COS-206 should define an embedding versioning strategy for brain.db: how policy embeddings are versioned alongside their generation model, how re-embedding is triggered when models change, and how retrieval degrades gracefully during a re-embedding transition.

**Priority 3: Policy Derivation Engine (COS-207)**
The policy derivation workflow described in Section 7.7 is a sketch. COS-207 should specify the full derivation algorithm: clustering method, minimum sample size, quality dominance threshold, cross-agent validation protocol, and promotion criteria. This is the most complex engineering work in the architecture and warrants dedicated research.

**Priority 4: Adversarial Robustness (COS-208)**
The risk of adversarial policy injection (Section 8, Question 5) is not hypothetical — any multi-agent system with a memory feedback loop is susceptible to strategic outcome manipulation. COS-208 should enumerate the threat model and propose mitigation: anomaly detection on outcome patterns, multi-source outcome validation, cryptographic signing of outcome records, and rate limits on policy reinforcement events from single agents.

**Priority 5: Policy Legibility and Explainability (COS-209)**
As policies become load-bearing in agent decision-making, there will be demands (operational, regulatory, or ethical) to explain why a policy says what it says. COS-209 should design a policy explanation interface: given a `policy_id`, produce a human-readable summary of the evidence behind it, including representative memories, outcome distributions, and the derivation steps. This work intersects with XAI (explainable AI) research on concept extraction from embedding spaces.

**Priority 6: Reinforcement Learning Integration (COS-210)**
The memory-policy architecture described here is a precursor to a full RL-based policy learning system. COS-210 should evaluate whether the outcome signal in `policy_invocations` is rich enough to serve as a reward signal for a lightweight policy gradient update, and if so, design the training loop. This would upgrade the system from empirical (batch-derived) policies to continuously-updated policies — approaching the third tier on the adaptivity spectrum in Section 3.4.

---

## 11. References

### Academic Foundations

**Policy-Based Reasoning and Decision Systems**

Boutilier, C., Dean, T., & Hanks, S. (1999). Decision-theoretic planning: Structural assumptions and computational leverage. *Journal of Artificial Intelligence Research*, 11, 1-94.

Russell, S. J., & Norvig, P. (2020). *Artificial Intelligence: A Modern Approach* (4th ed.). Pearson. [Chapters on rational agents and decision making under uncertainty]

Weiss, G. (Ed.). (2013). *Multiagent Systems* (2nd ed.). MIT Press. [Particularly Chapter 7: Interaction, Coordination, and Organization]

Wooldridge, M. (2009). *An Introduction to MultiAgent Systems* (2nd ed.). Wiley. [Chapter 10: Practical Reasoning Agents]

**Memory-Augmented Decision Making**

Tulving, E. (1985). Memory and consciousness. *Canadian Psychology/Psychologie canadienne*, 26(1), 1-12. [Foundational distinction between episodic and semantic memory — maps to experience memories vs. policy memories]

Graves, A., Wayne, G., Reynolds, M., Harley, T., Danihelka, I., Grabska-Barwinska, A., ... & Hassabis, D. (2016). Hybrid computing using a neural network with dynamic external memory. *Nature*, 538(7626), 471-476. [Neural Turing Machine — external memory for decision augmentation]

Weston, J., Chopra, S., & Bordes, A. (2015). Memory networks. *International Conference on Learning Representations (ICLR)*. arXiv:1410.3916.

**Reinforcement Learning from Memory**

Mnih, V., Kavukcuoglu, K., Silver, D., Rusu, A. A., Veness, J., Bellemare, M. G., ... & Hassabis, D. (2015). Human-level control through deep reinforcement learning. *Nature*, 518(7540), 529-533. [Experience replay as memory-driven policy learning]

Andrychowicz, M., Wolski, F., Ray, A., Schneider, J., Fong, R., Welinder, P., ... & Zaremba, W. (2017). Hindsight experience replay. *Advances in Neural Information Processing Systems (NeurIPS)*, 30. [Reinterpretation of failed outcomes as learning signal — directly relevant to Section 5]

Sutton, R. S., & Barto, A. G. (2018). *Reinforcement Learning: An Introduction* (2nd ed.). MIT Press. [Foundational RL theory, particularly Chapters 4-6 on policy improvement]

**Multi-Agent Coordination and Distributed Decision Making**

Lopes, M., Melo, F. S., & Montesano, L. (2009). Active learning for reward estimation in inverse reinforcement learning. *European Conference on Machine Learning (ECML)*, 31-46.

Shoham, Y., & Leyton-Brown, K. (2008). *Multiagent Systems: Algorithmic, Game-Theoretic, and Logical Foundations*. Cambridge University Press.

Tambe, M. (1997). Towards flexible teamwork. *Journal of Artificial Intelligence Research*, 7, 83-124.

**Policy Capture and Feedback Loop Pathologies**

Lerman, K. (2006). Social networks and social information filtering on Digg. *Proceedings of the International Conference on Weblogs and Social Media*. [Policy capture in collaborative filtering — structural analog]

Sculley, D., Holt, G., Golovin, D., Davydov, E., Phillips, T., Ebner, D., ... & Dennison, D. (2015). Hidden technical debt in machine learning systems. *Advances in Neural Information Processing Systems (NeurIPS)*, 28. [Policy brittleness and feedback loop degradation in production ML systems]

Quiñonero-Candela, J., Sugiyama, M., Schwaighofer, A., & Lawrence, N. D. (Eds.). (2009). *Dataset Shift in Machine Learning*. MIT Press. [Conceptual foundation for Section 4.2: stale policies under distribution shift]

**Retrieval-Augmented Generation and Semantic Search**

Lewis, P., Perez, E., Piktus, A., Petroni, F., Karpukhin, V., Goyal, N., ... & Kiela, D. (2020). Retrieval-augmented generation for knowledge-intensive NLP tasks. *Advances in Neural Information Processing Systems (NeurIPS)*, 33. [RAG architecture — semantic retrieval for decision augmentation]

Karpukhin, V., Oguz, B., Min, S., Lewis, P., Wu, L., Edunov, S., ... & Yih, W. T. (2020). Dense passage retrieval for open-domain question answering. *Empirical Methods in Natural Language Processing (EMNLP)*. [Dense retrieval methods applicable to policy retrieval]

**Organizational Learning and Knowledge Management**

Argyris, C., & Schön, D. A. (1978). *Organizational Learning: A Theory of Action Perspective*. Addison-Wesley. [Double-loop learning — directly maps to the policy update mechanism in Section 5]

Nonaka, I., & Takeuchi, H. (1995). *The Knowledge-Creating Company*. Oxford University Press. [Tacit-to-explicit knowledge conversion — policies as formalized organizational tacit knowledge]

Walsh, J. P., & Ungson, G. R. (1991). Organizational memory. *Academy of Management Review*, 16(1), 57-91. [Organizational memory as policy substrate]

### Implementation References

SQLite JSON functions documentation: https://www.sqlite.org/json1.html
SQLite FTS5 full-text search: https://www.sqlite.org/fts5.html
FAISS — Facebook AI Similarity Search (for embedding index): https://faiss.ai/
sqlite-vss — Vector similarity search extension for SQLite: https://github.com/asg017/sqlite-vss

---

*End of COS-204 Research Report*

*This report is part of the Cognitive Operating System (COS) research series for the Hermes multi-agent system. The COS series is maintained in `/agentmemory/research/` and indexed in `/agentmemory/research/index.md`.*
