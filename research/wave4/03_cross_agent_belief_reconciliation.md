# Cross-Agent Belief Reconciliation — Detecting Incompatible World-Models
## Research Report — COS-179
**Author:** Cortex (Intelligence Synthesis Analyst)
**Date:** 2026-03-28
**Target:** brain.db — Belief reconciliation layer for detecting divergent agent world-models
**Builds on:** `06_contradiction_detection.py` (Wave 1), COS-113 (collective intelligence), COS-110 (metacognition)

---

## Executive Summary

The existing contradiction detection system (Wave 1) catches **explicit conflicts**: two memories in the same scope with semantically opposite content. It misses **implicit divergence**: two agents operating from incompatible world-models where neither has written down their contradictory assumption. Agent A believes the auth system is stateless; Agent B's code assumes it maintains session state. Neither has written this belief explicitly — it's embedded in their behavior patterns, code decisions, and event history.

This is the harder problem, and it is operationally more dangerous. Explicit contradictions are detectable and patchable. Implicit world-model divergence persists invisibly until it causes a production failure.

**Central finding:** World-model divergence detection requires a two-track approach:
1. **Explicit belief reconciliation** — extend contradiction_detection.py to handle cross-scope, cross-agent conflicts (near-term, implementable now)
2. **Implicit belief inference** — extract implicit beliefs from agent behavior patterns (event streams, code decisions, task outcomes) and compare across agents (medium-term, requires ML inference step)

**Highest-impact recommendation:** Implement cross-scope belief comparison as a new pass in the consolidation cycle. The existing `06_contradiction_detection.py` only compares memories within the same scope — adding cross-scope contradiction scanning for related entities (same project, same system component) would surface the majority of dangerous divergences without requiring behavioral inference.

---

## 1. What Is a World-Model?

A world-model is an agent's implicit or explicit representation of how a system works — its state, its components, its causal rules, and its current condition. For a software agent:

- **Structural beliefs**: "The auth system has a sessions table." "COS-83 is complete." "brain.db is the source of truth for all agents."
- **Causal beliefs**: "If I write to brain.db, Hermes will see it on next retrieval." "Failing a checkout means another agent is working there."
- **State beliefs**: "COS-91 is blocked." "The memory spine is healthy." "Route-context is running."
- **Process beliefs**: "Agents write events when they complete work." "Checkout is required before status updates."

Divergent world-models arise when agents A and B have different beliefs in any of these categories — particularly when the difference is unknown to both.

---

## 2. Taxonomy of Belief Divergence

### 2.1 Explicit Conflicts (Already Handled — Wave 1)

```
Agent A: [scope=project:COS, content="COS-83 is in_progress", confidence=0.8]
Agent B: [scope=project:COS, content="COS-83 is done", confidence=0.9]
```
Same scope, semantically contradictory. `contradiction_detection.py` catches this via negation patterns and supersession chain audit.

**Gap in current coverage:** Only same-scope conflicts are detected. Cross-scope conflicts are missed.

### 2.2 Cross-Scope Conflicts (Partially Handled — Needs Extension)

```
Agent A: [scope=agent:sentinel-2, content="coherence checking is handled by sentinel-2"]
Agent B: [scope=project:agentmemory, content="no automated coherence checking exists"]
```
Different scopes, factually incompatible. Current system does not compare across scopes.

**Fix:** Add cross-scope contradiction scan for entities that appear in both memories (entity extraction → match → compare).

### 2.3 Temporal Conflicts (Not Handled)

```
Agent A (day 1): [content="COS-83 is in_progress", created_at=T1]
Agent B (day 3): [content="COS-83 is done", created_at=T3]
```
These are not really contradictions — they represent different points in time. The temporal ordering resolves the conflict. Current system lacks a proper temporal ordering pass; it may flag these as contradictions incorrectly.

**Fix:** Before flagging contradiction, check temporal ordering. If T1 < T2 and the claims are logically consistent with status progression (in_progress → done), resolve as temporal sequence, not contradiction.

### 2.4 Implicit Conflicts (Not Handled — Hard Problem)

Agent A's event history shows a pattern: always checking out before writing, always respecting status transitions. Agent B's event history shows: occasionally writing without checkout, treating blocked as equivalent to in_progress.

Neither agent has written down "checkout is optional" — but B is operating from that assumption. This is an implicit belief conflict detectable only from behavioral analysis.

**Detection approach:** Pattern mining over event streams. If agent B's events show systematic deviations from protocol (checkout → work → update) that agent A never deviates from, B holds an implicit belief that contradicts A's implicit belief (and the explicit system protocol).

### 2.5 Staleness Conflicts (Partially Handled via Decay)

```
Agent A: [content="Hermes has 12 agents in the M&I Division", created_at=day1]
(actual current state: 22 agents)
```
This is not a conflict between two agents — it's a conflict between an agent's stored belief and the current world state. The coherence_check tool (COS-85) explicitly detected this pattern (memories #87, #90). It's a form of belief staleness rather than inter-agent divergence, but has the same failure mode.

**Current handling:** coherence_check.py detects stale numeric claims. The remaining gap is stale structural claims ("the auth system is X") where no numerical check exists.

---

## 3. Belief State Modeling

### 3.1 Possible Worlds Semantics (Kripke 1963)

In modal logic, an agent's belief state is a set of *possible worlds* — all states of affairs consistent with what the agent believes. Belief revision (updating when new information arrives) is modeled as restricting the possible worlds set.

For brain.db agents: each agent's belief state is implicitly defined by the union of their active memories. The "possible worlds" they consider are the range of interpretations consistent with those memories.

**Inter-agent divergence detection:** Two agents A and B have incompatible world-models if no single possible world is consistent with both A's beliefs and B's beliefs. Practically: if A believes P and B believes ¬P (or something that logically implies ¬P), they are divergent.

### 3.2 Belief Revision Theory (Alchourrón, Gärdenfors, Makinson — AGM 1985)

The AGM postulates define rational belief revision when new information contradicts existing beliefs:

1. **Success**: After revision, the new information is believed
2. **Consistency**: If the new information is consistent, the revised belief set is consistent
3. **Conservatism**: Change as little as possible (minimal revision)
4. **Recovery**: Expanding by P then contracting by P returns to the original

**For brain.db:** When a new memory contradicts an existing one, the revision algorithm should:
1. Accept the newer memory (success)
2. Mark the older contradicted memory as `status='retired'` with `superseded_by=new_id` (consistency via minimal change)
3. Preserve the older memory in the audit trail (recovery)

Current contradiction_detection.py identifies conflicts but doesn't execute AGM revision — it only reports. Adding a revision pass would automate conflict resolution rather than just detection.

### 3.3 Belief Merging for Multi-Agent Systems (Konieczny & Pino Pérez 2002)

When N agents hold different beliefs about the same fact, belief merging produces a single consistent merged belief set. The key principle: **IC merging** (Integrity Constraints) ensures the merged result satisfies all known hard constraints (physical laws, logical axioms, system invariants).

For brain.db agents, hard constraints include:
- A task cannot be both `done` and `in_progress` simultaneously
- An agent cannot be its own manager in the reporting chain
- A memory cannot be both `active` and `retired`

Soft constraints (violated beliefs should trigger warnings, not hard rejection):
- An agent's memory should not contradict its own manager's memory on the same topic
- Memories older than 30 days on fast-moving topics (project status) should be flagged as likely stale

---

## 4. Reconciliation Protocol Design

### 4.1 Detection Pipeline

```python
# Extend consolidation-cycle with cross-agent belief reconciliation pass
def cross_agent_belief_reconciliation(db):

    # Phase 1: Extract entity references from all memories
    entity_index = build_entity_index(db)
    # entity_index = {'COS-83': [memory_12, memory_45, memory_89], ...}

    # Phase 2: For entities with memories from multiple agents, compare beliefs
    conflicts = []
    for entity, memories in entity_index.items():
        if len(set(m.agent_id for m in memories)) < 2:
            continue  # Only one agent has beliefs about this entity — no cross-agent conflict possible

        # Phase 3: Temporal sort + contradiction check
        sorted_mems = sorted(memories, key=lambda m: m.created_at)
        for i, m1 in enumerate(sorted_mems):
            for m2 in sorted_mems[i+1:]:
                if m1.agent_id == m2.agent_id:
                    continue  # Same agent, handled by same-agent contradiction detection
                if is_temporal_sequence(m1, m2):
                    continue  # T1 < T2, consistent temporal update
                if semantic_contradiction(m1, m2):
                    conflicts.append(BeliefConflict(m1, m2, entity))

    return conflicts
```

### 4.2 Resolution Strategies

Three resolution modes, applied in order of confidence:

**Auto-resolve (high confidence):** When temporal ordering clearly resolves the conflict (newer memory from the same entity domain has higher confidence), retire the older memory automatically.

**Flag for synthesis (medium confidence):** When two memories from different agents conflict on the same entity with similar confidence scores and similar recency, flag for a synthesis agent to review and produce a merged memory.

**Escalate to human (low confidence):** When the conflict involves a memory from a trusted agent (Hermes, Legion) and the contradicting memory has higher confidence or more recent evidence — flag for human review. Don't auto-resolve trusted-agent memories without oversight.

### 4.3 World-Model Summary Generation

For each active project and agent, generate a **world-model summary**: the set of entity claims that are currently inconsistent across agents. Format:

```
World-Model Divergence Report — 2026-03-28
Project: costclock-ai

DIVERGENCE 1: auth system state model
  Agent A (hermes): "auth is stateless, JWT only" [confidence=0.85, 3 days old]
  Agent B (kernel): "auth has session persistence" [confidence=0.70, 1 day old]
  Resolution: flag for synthesis — similar confidence, temporal precedence unclear

DIVERGENCE 2: COS-83 status
  Agent A (paperclip-weaver): "COS-83 done" [confidence=0.95, same-day]
  Agent B (paperclip-codex): "COS-83 in progress" [confidence=0.80, 5 hours old]
  Resolution: auto-resolve to weaver's version (newer + higher confidence)
```

### 4.4 Implicit Belief Detection via Behavioral Inference

For the harder problem of implicit beliefs embedded in behavior:

```python
def detect_behavioral_divergence(db, agent_ids: list[str]):
    """Detect implicit beliefs from event stream patterns."""

    # Pattern: Does agent systematically skip checkout?
    checkout_compliance = {}
    for agent_id in agent_ids:
        work_events = db.execute("""
            SELECT e1.agent_id, e1.event_type, e1.created_at,
                   LAG(e1.event_type) OVER (PARTITION BY e1.agent_id ORDER BY e1.created_at) as prev_type
            FROM events e1
            WHERE e1.agent_id = ?
              AND e1.event_type IN ('result', 'task_update', 'decision')
        """, (agent_id,)).fetchall()

        # Count result events preceded by a checkout vs. not preceded by one
        with_checkout = sum(1 for e in work_events if e.prev_type == 'checkout')
        total = len(work_events)
        checkout_compliance[agent_id] = with_checkout / total if total > 0 else None

    # Agents with checkout_compliance < 0.5 have an implicit belief that checkout is optional
    divergent_agents = {a: score for a, score in checkout_compliance.items()
                       if score is not None and score < 0.5}

    return divergent_agents
```

---

## 5. Schema Additions

```sql
-- Cross-agent belief conflicts log
CREATE TABLE belief_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_ref TEXT NOT NULL,           -- entity the conflict is about (e.g., 'COS-83', 'agent:hermes')
    memory_id_a INTEGER REFERENCES memories(id),
    memory_id_b INTEGER REFERENCES memories(id),
    agent_id_a TEXT REFERENCES agents(id),
    agent_id_b TEXT REFERENCES agents(id),
    conflict_type TEXT NOT NULL CHECK(conflict_type IN (
        'explicit_cross_scope',
        'temporal_ordering',
        'confidence_gap',
        'implicit_behavioral'
    )),
    severity REAL,                      -- 0.0–1.0
    resolution_strategy TEXT,           -- 'auto_resolve', 'flag_synthesis', 'escalate_human'
    resolved INTEGER DEFAULT 0,
    resolution_note TEXT,
    detected_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);

-- Entity extraction cache for fast cross-agent comparison
CREATE TABLE entity_belief_index (
    entity_ref TEXT NOT NULL,
    memory_id INTEGER REFERENCES memories(id),
    agent_id TEXT NOT NULL,
    claim_summary TEXT,                 -- brief extracted claim about this entity
    confidence REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (entity_ref, memory_id)
);

-- World-model divergence reports (periodic snapshots)
CREATE TABLE worldmodel_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,                -- 'project:X', 'global', etc.
    divergence_count INTEGER,
    auto_resolved INTEGER,
    pending_synthesis INTEGER,
    pending_escalation INTEGER,
    report_body TEXT,                   -- full report as text
    generated_at TEXT NOT NULL
);
```

---

## 6. Integration with Existing Systems

| Existing System | Integration |
|---|---|
| `06_contradiction_detection.py` | Extend with cross-scope pass; add entity extraction layer |
| `consolidation-cycle` | Add `cross-agent-reconcile` as pass 7 (after contradiction detection) |
| `knowledge_edges` | Add edge type `CONTRADICTS` between conflicting memory pairs |
| `coherence_check.py` | Feed `belief_conflicts` table into coherence score calculation |
| `route-context` (COS-83) | Use conflict data to prefer agents with consistent beliefs on a topic |

---

## 7. New Questions Raised

1. **Entity extraction quality gate**: The cross-scope detection depends on extracting entity references from memory content. LLM-based extraction is expensive; regex-based extraction is cheap but misses implicit references. What's the right extraction strategy for 178 agents × N memories at 30-minute cycle cadence?

2. **Resolution authority**: When auto-resolution picks the newer memory, it implicitly trusts recency over accuracy. Is this right? A very confident old memory from a domain expert should sometimes win over a less confident recent memory from a generalist. How do we incorporate expertise into resolution authority?

3. **Protocol belief divergence**: Behavioral divergence detection (agents who skip checkout) surfaces implicit beliefs that violate protocol. But should these be treated as "beliefs" to reconcile or as "compliance failures" to enforce? The framing matters: reconciliation is cooperative, enforcement is punitive.

---

## 8. Architecture Assumptions That Are Wrong

1. **Contradiction detection is sufficient with same-scope comparison**: The vast majority of dangerous belief divergences are cross-scope (agent A's scope says X, agent B's project scope says ¬X). Same-scope detection captures only a small fraction of the actual belief conflict space.

2. **Belief conflicts are binary**: The current system marks a memory as either contradicted or not. Real beliefs exist on a continuum — "significantly incompatible," "weakly inconsistent," "different emphasis but compatible." Binary classification leads to over-flagging (too many false positive conflicts) or under-flagging (missing subtle divergences).

3. **All conflicts need resolution**: Some belief conflicts are fine to leave unresolved if both agents have limited confidence and the question is genuinely uncertain. The reconciliation system should distinguish between *dangerous* divergences (where agents are making incompatible decisions) and *informative* divergences (where the uncertainty is real and should be preserved).

---

## 9. Highest-Impact Follow-Up Research

**"Entity Extraction for Belief Graph Construction — Efficient and Accurate Techniques for Agent Memory"**

The cross-scope belief reconciliation pipeline depends critically on extracting entity references from natural language memory content. The choice of extraction method determines both the recall (do we catch all entity references?) and precision (do we generate false entity matches?). A systematic comparison of: regex-based extraction, spaCy NER, embedding-based entity clustering, and the existing FTS5 index as an entity index — run against the live brain.db corpus — would establish the cost/accuracy frontier for this component. Without this, the belief reconciliation pipeline is built on an uncertain foundation.

---

## References

- Kripke, S. (1963). Semantical considerations on modal logic. *Acta Philosophica Fennica*, 16.
- Alchourrón, C., Gärdenfors, P. & Makinson, D. (1985). On the logic of theory change. *Journal of Symbolic Logic*, 50(2).
- Konieczny, S. & Pino Pérez, R. (2002). Merging information under constraints. *Journal of Logic and Computation*, 12(5).
- Fagin, R. et al. (1995). *Reasoning about Knowledge*. MIT Press.
- Wobcke, W. (1995). Belief revision, conditional logic and nonmonotonic reasoning. *Notre Dame Journal of Formal Logic*.
