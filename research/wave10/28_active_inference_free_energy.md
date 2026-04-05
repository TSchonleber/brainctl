# Active Inference & Free Energy — Friston's Framework for Agent Decision-Making

**Author:** Cortex (Intelligence Synthesis Analyst)
**Task:** [COS-341](/COS/issues/COS-341)
**Date:** 2026-03-28
**DB State:** 22 active agents · 9 active memories · brain.db @ ~/agentmemory/db/brain.db

---

## Executive Summary

Our agent fleet is reactive: agents wait for task assignment, execute, and report. This leaves a class of failures entirely unaddressed — failures caused by *information gaps that the agent could have anticipated and filled*. Friston's Active Inference framework reframes cognition as prediction-error minimization: intelligent systems don't just respond to the world, they actively sample the world to reduce uncertainty.

Applied to our architecture, this yields a concrete design: an **Active Inference Layer (AIL)** where agents, before and during task execution, autonomously query `brain.db` to fill anticipated knowledge gaps — rather than waiting for Hermes to push context. This document maps each Fristonian concept to a concrete implementation against our existing infrastructure.

**Key design claim:** Active Inference requires no new data structures. Every concept maps to existing `brain.db` fields (`trust_score`, `confidence`, `importance`, `knowledge_edges`) with one new table (`agent_uncertainty_log`) and two new `brainctl` commands (`brainctl infer pre-task`, `brainctl infer gap-fill`).

---

## 1. The Free Energy Principle — Minimize Prediction Error

### Friston's Claim

An intelligent system minimizes **variational free energy** — a mathematical bound on surprise (how unexpected sensory inputs are). Free energy = prediction error + complexity penalty. Agents that minimize free energy are simultaneously:
- Accurate (their predictions match what they observe)
- Parsimonious (they don't build overly complex internal models)

### The Problem This Solves for Us

When an agent receives a task, it currently retrieves whatever memories surface from a keyword search. If the search misses a critical piece of knowledge — because the agent didn't know to look for it — the agent proceeds into the task with unacknowledged uncertainty. Failures follow.

Example: Kokoro assigns a PR merge task. The agent searches "PR merge" and finds recent patterns. But it misses the memory that the relevant branch has an active merge freeze (COS-322-era finding). The agent proceeds, the merge fails, Hermes has to intervene.

Active Inference says: *before executing, the agent should estimate what it doesn't know and actively seek to fill those gaps.*

### Mapping to brain.db

| Fristonian Concept | brain.db Implementation |
|---|---|
| Prediction error | `confidence < 0.7` on retrieved memories = high prediction error signal |
| Complexity penalty | Memory retrieval cost (token budget per `brainctl push run`) |
| Free energy | `prediction_error * importance` — weighted surprise for each knowledge gap |
| Minimization | Iterative retrieval until uncertainty budget exhausted or gaps filled |

**Implementation:** Add a pre-task scan pass:

```python
def estimate_free_energy(task_description: str, agent_id: str) -> list[UncertaintyGap]:
    """
    Before starting a task, query brain.db for relevant memories.
    Flag low-confidence retrievals as uncertainty gaps.
    Return gaps sorted by (1 - confidence) * importance.
    """
    memories = brainctl_push_run(agent_id, task_description, limit=10)
    gaps = []
    for m in memories:
        prediction_error = 1.0 - m.confidence
        free_energy = prediction_error * m.importance
        if free_energy > UNCERTAINTY_THRESHOLD:
            gaps.append(UncertaintyGap(memory_id=m.id, free_energy=free_energy, topic=m.content[:100]))
    return sorted(gaps, key=lambda g: g.free_energy, reverse=True)
```

**New `brainctl` command:** `brainctl infer pre-task "<task description>"` — runs the free energy scan and outputs the top-N uncertainty gaps the agent should resolve before starting.

---

## 2. Epistemic Foraging — Explore When Uncertain, Exploit When Confident

### Friston's Claim

Intelligent systems face the classic exploration/exploitation tradeoff. Friston's answer: the switch is *epistemic value* — the expected information gain from an action. When epistemic value is high (you'd learn a lot by exploring), explore. When it's low (you already know), exploit.

### Mapping to Our System

Our agents currently have no confidence-aware retrieval strategy. Every heartbeat uses the same `brainctl search` with the same parameters regardless of how well the agent knows the domain.

**Proposed policy:** Dynamic retrieval depth based on agent's domain confidence.

```python
CONFIDENCE_THRESHOLDS = {
    "exploit":  0.85,   # High confidence: use top-3 memories, skip graph expansion
    "balanced": 0.65,   # Medium confidence: top-5 + 1 graph hop
    "explore":  0.00,   # Low confidence: top-10 + 2 graph hops + request peer memories
}

def get_retrieval_strategy(domain_confidence: float) -> RetrievalConfig:
    if domain_confidence >= 0.85:
        return RetrievalConfig(limit=3, no_graph=True, peer_query=False)
    elif domain_confidence >= 0.65:
        return RetrievalConfig(limit=5, graph_hops=1, peer_query=False)
    else:
        return RetrievalConfig(limit=10, graph_hops=2, peer_query=True)
```

**Domain confidence** is estimated from:
1. Mean `confidence` of top-N retrieved memories for the task keywords
2. Agent's own `agent_beliefs` entry for the relevant domain (already live from COS-318)

**Epistemic foraging in practice:**
- A new agent on a domain it hasn't encountered → explore mode → 10 memories + peer query to Hermes/domain expert
- A senior agent on a task it's done 50 times → exploit mode → 3 memories, no graph, fast execution
- Protocol overhead savings: exploit mode costs ~105 tokens (push run); explore mode costs ~2,000 tokens but only when genuinely needed

**Graph structure advantage:** The `knowledge_edges` table (2,675 edges, Wave 1 knowledge graph) is the natural substrate for epistemic foraging. Each hop discovers semantically adjacent memories the keyword search didn't surface. Uncertainty reduction via graph traversal.

---

## 3. Expected Free Energy — Planning by Minimizing Anticipated Surprise

### Friston's Claim

An agent planning a sequence of actions should select the plan that minimizes *expected* free energy — not just current surprise, but predicted future surprise. This is how active inference handles goal-directed behavior: goals are treated as prior beliefs about desired future states. Deviations from goal states generate prediction error; the agent acts to minimize those deviations.

### Mapping to Our System

Before executing a complex task, an agent should simulate what *could go wrong* (information gaps that might surface mid-task) and pre-fill them.

**Design: Pre-Task Simulation Protocol**

1. **Parse the task** into a dependency graph of likely sub-operations (e.g., "create PR" → branch check → merge conflict check → reviewer availability check → CI status check)
2. **Query brain.db** for each sub-operation: what failures have occurred at this step historically?
3. **Rank sub-operations by expected free energy** = `P(failure) * cost(failure)`
4. **Pre-fill the highest-risk gaps** before starting

```python
def simulate_task_free_energy(task: str, agent_id: str) -> SimulationResult:
    """
    Decompose task into steps, estimate failure probability per step,
    return pre-fill recommendations ordered by expected cost.
    """
    steps = decompose_task(task)  # Heuristic or LLM-based
    risks = []
    for step in steps:
        # Query brain.db for past failures at this step
        failure_memories = brainctl_search(
            agent_id, f"{step} failure error blocked",
            category="event", limit=5, no_graph=True
        )
        p_failure = len([m for m in failure_memories if m.confidence > 0.7]) / 5.0
        risks.append(StepRisk(step=step, p_failure=p_failure, memories=failure_memories))

    return SimulationResult(
        steps=steps,
        risks=sorted(risks, key=lambda r: r.p_failure, reverse=True),
        recommended_prefill=[r.step for r in risks if r.p_failure > 0.3]
    )
```

**World model integration:** COS-321 delivered a `world_model` table with `brainctl world predict`. This is the natural substrate for expected free energy simulation. `brainctl world predict --what "merge PR for COS-341"` should surface predicted blockers from the org simulation layer.

**New `brainctl` command:** `brainctl infer gap-fill "<task>"` — runs simulation, identifies top-3 highest-risk steps, and queries memories to pre-fill those gaps. Output: a compact pre-task briefing (~200 tokens).

---

## 4. Precision Weighting — Attend More to Reliable Information

### Friston's Claim

Not all sensory signals are equally reliable. Friston's framework includes **precision weighting**: each signal is multiplied by its estimated reliability (precision = inverse variance). High-precision signals dominate inference; low-precision signals are discounted. This is the mechanism behind attention in predictive coding.

### Mapping to Our System

We already have `trust_score` on memories. It is *not* currently used as a retrieval weight — it's stored but only referenced in provenance chains (COS-121). This is a missed precision weighting opportunity.

**Proposed schema change:** Add precision-weighted scoring to `brainctl search`.

Current retrieval scoring (Wave 1 attention/salience routing):
```
score = 0.45 × semantic_sim + 0.25 × recency + 0.20 × confidence + 0.10 × importance
```

Proposed precision-weighted scoring:
```
precision = trust_score × confidence
score = 0.40 × semantic_sim + 0.25 × recency + 0.20 × confidence + 0.10 × importance + 0.05 × precision
```

**Why the small weight (0.05)?** Trust score is a meta-signal — it reflects source reliability, not content relevance. Over-weighting it risks filtering out accurate-but-untrusted memories. The goal is a tiebreaker for equally-relevant memories, not a hard gate.

**Precision weighting in multi-agent context:** When Agent A retrieves a memory originally written by Agent B, the `trust_score` on that memory encodes how reliable B's outputs are. Agents that have historically produced contradicted or superseded memories get lower trust — their contributions are downweighted in future retrievals. This is organizational precision weighting.

**Implementation path:**
1. Add `precision = trust_score * confidence` as a computed column or inline in retrieval query
2. Add `--precision-weighted` flag to `brainctl search` (or make it default with `--no-precision` opt-out)
3. No schema migration required; `trust_score` and `confidence` already exist

---

## 5. Active Sensing in Multi-Agent Systems

### Friston's Claim

In multi-agent settings, active inference extends to *active sensing* — agents don't just reduce their own uncertainty, they coordinate to reduce *collective* uncertainty. Agents should share information that would reduce other agents' prediction error, not just their own.

### Mapping to Our 26-Agent Fleet (Targeting 178)

Currently, information sharing is:
- **Pull:** Agents query brain.db on demand
- **Push:** Hermes manually pushes critical memories
- **Broadcast:** Global workspace (COS-314) for salience-threshold events

Missing: **targeted epistemic push** — if Agent A resolves a knowledge gap that Agent B is likely to encounter, A should proactively push that knowledge to B's relevant scope.

**Design: Collective Uncertainty Reduction (CUR) Protocol**

1. When an agent resolves a knowledge gap (writes a new memory or event), it estimates *which other agents are likely to encounter the same gap* using:
   - `knowledge_edges` — agents sharing topic edges
   - `agent_beliefs` — agents with the same domain expertise
   - `world_model` — agents currently working on related tasks

2. If predicted benefit > push cost, the agent writes to shared scope (not agent-local scope)

3. Alternatively: tag the memory with `epistemic_flag = True` to signal "this resolved a non-obvious gap; other agents should be aware"

```python
def should_push_collective(memory: Memory, resolving_agent: str) -> list[str]:
    """
    After resolving a knowledge gap, identify other agents likely to face the same gap.
    Return list of agent_ids for targeted memory push.
    """
    # Find agents with overlapping knowledge graph edges
    related_agents = brainctl_knowledge_graph_neighbors(memory.id, max_hops=2)
    # Filter to agents currently active on related tasks (via world model)
    active_related = [a for a in related_agents if world_model_is_active(a)]
    # Only push if benefit estimate > overhead
    return [a for a in active_related if epistemic_value(memory, a) > PUSH_THRESHOLD]
```

**At 178 agents**, this becomes essential. Without collective uncertainty reduction, each agent independently rediscovers the same gaps. With it, one agent's resolution propagates to all agents likely to face the same uncertainty.

**Integration with reflexion propagation (COS-320):** The `propagated_to` column from COS-320's migration (`019_reflexion_propagation.sql`) is the right substrate. Reflexion lessons are exactly the resolved-knowledge-gap case: "I made this mistake; here's the corrective memory." Precision-weighted, epistemic-flagged reflexion propagation = Active Inference for the fleet.

---

## 6. Proposed Active Inference Layer (AIL) Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    ACTIVE INFERENCE LAYER (AIL)                  │
│                                                                   │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────┐  │
│  │  Pre-Task Scan  │    │   Epistemic     │    │  Collective │  │
│  │  (Free Energy   │    │   Foraging      │    │  Uncertainty│  │
│  │   Estimation)   │    │   (Explore vs   │    │  Reduction  │  │
│  │                 │    │    Exploit)     │    │             │  │
│  │ brainctl infer  │    │                 │    │  Propagate  │  │
│  │  pre-task       │    │ dynamic limit + │    │  resolved   │  │
│  │                 │    │ graph hops      │    │  gaps to    │  │
│  └────────┬────────┘    └────────┬────────┘    │  likely     │  │
│           │                     │             │  recipients │  │
│           ▼                     ▼             └──────┬──────┘  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │              brain.db (Existing Infrastructure)              ││
│  │  trust_score · confidence · importance · knowledge_edges     ││
│  │  agent_beliefs · world_model · reflexion propagation        ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

### New schema requirement: `agent_uncertainty_log`

```sql
CREATE TABLE agent_uncertainty_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    task_desc   TEXT,
    gap_topic   TEXT,            -- what the agent didn't know
    free_energy REAL,            -- (1 - confidence) * importance at time of gap
    resolved_at TIMESTAMP,       -- when the gap was filled
    resolved_by INTEGER REFERENCES memories(id),  -- memory that filled it
    propagated  BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

This table gives us empirical data on what agents actually don't know — the ground truth for improving future retrieval and for identifying systematic knowledge gaps in the fleet.

---

## 7. Implementation Roadmap

| Phase | What | New Infrastructure | Effort |
|---|---|---|---|
| **Phase 1** (low-risk) | Precision weighting in retrieval | `--precision-weighted` flag in `brainctl search` | Low |
| **Phase 1** (low-risk) | `agent_uncertainty_log` table | Migration + schema | Low |
| **Phase 2** (medium) | `brainctl infer pre-task` | New command, free energy scan | Medium |
| **Phase 2** (medium) | Dynamic retrieval strategy (explore/exploit) | Agent confidence → retrieval config | Medium |
| **Phase 3** (high value) | `brainctl infer gap-fill` | Task simulation + pre-fill | High |
| **Phase 3** (high value) | Collective uncertainty reduction | Post-write propagation logic | High |

**Recommended sequence:** Phase 1 first — precision weighting is zero-schema-change and pays dividends immediately. `agent_uncertainty_log` enables measurement. Phase 2 and 3 build on the log data.

---

## 8. Risks & Constraints

| Risk | Severity | Mitigation |
|---|---|---|
| Pre-task scan adds latency to every heartbeat | Medium | Run async in background; cap at 300ms; skip if context budget > 80% used |
| Collective uncertainty reduction causes write contention | Medium | Write-gate: only push if agent_uncertainty_log shows > 2 other agents hit same gap |
| `agent_uncertainty_log` grows unbounded | Low | Add to hippocampus decay scope; ephemeral temporal class; 30-day TTL |
| Domain confidence estimation is noisy | Low | Bootstrap from `agent_beliefs` (COS-318); improve with empirical log data |
| Explore mode retrieves 10 memories + 2 graph hops → 5-8K tokens | Medium | Hard cap: explore mode max 12K tokens; fallback to balanced if context tight |

---

## 9. Connection to Existing Research

| Prior Work | Connection |
|---|---|
| Wave 1: Attention/salience routing | Foundation for precision-weighted scoring |
| Wave 1: Knowledge graph (COS-84) | Substrate for epistemic foraging graph hops |
| Wave 2: Predictive Cognition (COS-112) | Overlapping; predictive cognition is expected free energy estimation |
| Wave 3: Proactive Memory Push (COS-124) | Direct predecessor; AIL makes push criteria principled |
| Wave 3: Situation Model (COS-123) | Situation model = agent's current generative model; active inference updates it |
| Wave 8: Agent Beliefs (COS-318) | Domain confidence source for explore/exploit switch |
| Wave 8: Global Workspace (COS-314) | Broadcast channel for high-salience resolved gaps |
| Wave 9: Cross-agent Reflexion (COS-320) | Collective uncertainty reduction substrate |
| Wave 9: World Model (COS-321) | Expected free energy simulation substrate |

---

## 10. Conclusion

Active Inference reframes our agents from reactive executors to proactive reasoners. The five Fristonian concepts map cleanly to our existing infrastructure:

1. **Free Energy Minimization** → `brainctl infer pre-task` (estimate uncertainty before starting)
2. **Epistemic Foraging** → Dynamic retrieval depth based on `agent_beliefs` domain confidence
3. **Expected Free Energy** → `brainctl infer gap-fill` (simulate task, pre-fill highest-risk gaps)
4. **Precision Weighting** → `trust_score × confidence` as a retrieval scoring term
5. **Active Sensing** → Collective uncertainty reduction via post-write propagation

The full AIL requires one new table (`agent_uncertainty_log`), two new `brainctl` commands, and a scoring adjustment. All of Phase 1 can land without schema migration. The framework turns 26 (or 178) reactive agents into a self-organizing epistemic collective that actively reduces organizational uncertainty.

**Recommended next step:** File implementation tickets for Phase 1 (precision weighting + uncertainty log) and assign to Engram (schema) and Recall (retrieval scoring).
