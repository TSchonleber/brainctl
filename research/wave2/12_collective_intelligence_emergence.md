# Collective Intelligence Emergence — Making 178 Agents Smarter Than the Sum of Parts
## Research Report — COS-113
**Author:** Cortex (Intelligence Synthesis Analyst)
**Date:** 2026-03-28
**Target:** brain.db — Collective intelligence infrastructure enabling emergent insights across 178 agents

---

## Executive Summary

178 agents sharing a memory spine should be smarter than any individual agent. Currently, they are not — they share storage but not cognition. Each agent reads and writes independently with no mechanisms for: detecting when multiple agents have converged on the same insight, aggregating diverse beliefs into a stronger collective view, or generating insights that emerge only from cross-agent synthesis that no single agent could produce.

This report synthesizes six research frameworks — swarm intelligence, wisdom of crowds, transactive memory, network topology, computational social choice, and evolutionary epistemology — and proposes a concrete collective intelligence infrastructure for brain.db.

**Central finding:** The highest-leverage intervention is not a complex consensus algorithm or multi-agent communication protocol. It is **transactive memory completion**: building an explicit map of which agent knows what, so that queries can be routed to the agent (or combination of agents) most likely to have the answer. Wegner's transactive memory research shows that groups outperform individuals not because they all know everything, but because they know *who knows what* and route accordingly. We have 178 specialists with no directory.

**Highest-impact recommendation:** Implement an `agent_capability_index` in brain.db — a structured mapping of each agent's domain expertise derived from their event and memory history. Combine with the existing route-context routing (COS-83) to enable capability-aware query routing. This is primarily a data pipeline, not a new algorithm.

---

## 1. Swarm Intelligence — Local Rules, Global Intelligence

### 1.1 Ant Colony Optimization (ACO)

Ant colonies solve complex optimization problems (shortest path, resource allocation) via **stigmergy**: indirect coordination through environment modification. Ants deposit pheromones on paths they travel; stronger pheromone trails attract more ants, reinforcing effective routes while less-traveled paths decay.

**The key insight:** No ant has a global view. The global optimum emerges from local rules applied by many agents to a shared environment.

**brain.db equivalent — Stigmergic Memory:**
The memory spine is the shared environment. When agents repeatedly write to overlapping scopes, those scopes accumulate more memories (higher density) and receive more retrieval attention (higher recall_count). This is stigmergy already happening implicitly.

**What we're missing:** The pheromone decay mechanism. In ACO, old paths fade unless reinforced. brain.db has decay (temporal classes, confidence decay) but no *reinforcement through collective use*. When 10 agents all retrieve the same memory, that retrieval frequency should strengthen the memory and the paths leading to it — not just record individual access_log entries.

**Proposed mechanism:** Aggregate `access_log` writes across agents for each memory record. When multiple distinct agents have retrieved a memory in the same time window, apply a **collective recall boost** (multiplicative, not additive) to confidence. This creates emergent salience: memories that many agents find useful become more durable.

```python
# Collective reinforcement: memories recalled by 3+ distinct agents within 24h get +0.1 confidence
def apply_collective_reinforcement(db, window_hours=24):
    hot_memories = db.execute("""
        SELECT memory_id, COUNT(DISTINCT agent_id) as distinct_agents
        FROM access_log
        WHERE accessed_at > datetime('now', ? || ' hours')
        GROUP BY memory_id
        HAVING COUNT(DISTINCT agent_id) >= 3
    """, (-window_hours,)).fetchall()

    for row in hot_memories:
        boost = 0.05 * (row.distinct_agents - 2)  # +0.05 per agent beyond the 2nd
        db.execute("UPDATE memories SET confidence = MIN(1.0, confidence + ?) WHERE id=?",
                   (boost, row.memory_id))
```

### 1.2 Bee Waggle Dance — Quality-Weighted Broadcasting

Scout bees communicate food source quality through waggle dance duration: better sources get longer dances, attracting more foragers. The mechanism combines **quality weighting** with **redundant signaling** to amplify good discoveries.

**Applied to Hermes:** When an agent completes a task and logs a `result` event, the quality of that result should determine how widely it is broadcast to other agents. High-quality, high-confidence results should trigger a "waggle dance" — pushing the key insights to related agents' context queues (the proactive push mechanism from COS-124).

Currently, all results are logged identically regardless of quality. Adding importance weighting to the push mechanism creates a waggle-dance equivalent: the best discoveries propagate most, the routine ones stay local.

### 1.3 Fish Schooling — Emergent Coherence Without a Leader

Fish school without any fish having a global view. Each fish follows three local rules:
1. **Separation**: avoid crowding neighbors
2. **Alignment**: steer toward average heading of neighbors
3. **Cohesion**: steer toward average position of neighbors

The emergent behavior (coherent school) is not programmed — it falls out of local rules.

**Applied to agent memory:** What are the local rules that, applied consistently by 178 agents, would produce globally coherent memory?

1. **Separation**: don't write what others have already written (deduplication check before write)
2. **Alignment**: tag with the same scope/category conventions as neighboring agents working on the same project
3. **Cohesion**: include explicit `refs` linking new memories to related memories already in the store

These three rules applied consistently would produce emergent knowledge graph coherence without requiring a central coordinator.

---

## 2. Wisdom of Crowds — When Aggregation Beats Experts

### 2.1 Surowiecki's Four Conditions

Surowiecki's *The Wisdom of Crowds* (2004) identifies four conditions for accurate collective judgment:

1. **Diversity of opinion**: each agent brings independent information (not all reading the same input)
2. **Independence**: agents don't influence each other's answers before aggregating
3. **Decentralization**: agents draw on local, specialized knowledge
4. **Aggregation**: there is a mechanism to combine individual judgments into a collective decision

**Current state against each condition:**

| Condition | Current State | Problem |
|---|---|---|
| Diversity | High — 178 agents with different roles | ✅ Good |
| Independence | Medium — agents read shared brain.db, influencing each other | ⚠️ Partial |
| Decentralization | High — agents are domain specialists | ✅ Good |
| Aggregation | **None** — no mechanism combines agent views | ❌ Missing entirely |

**The aggregation gap is the critical missing piece.** The system has diverse, independent, decentralized knowledge production but zero aggregation. Adding even a simple majority-vote or confidence-weighted average over conflicting memories would create wisdom-of-crowds dynamics.

### 2.2 Conditions for Crowd Failure

Surowiecki also identifies when crowds fail (this is operationally important):

- **Homogeneity**: when all agents are trained on the same data and have the same biases, crowd diversity is illusory
- **Cascades**: when agents update beliefs based on observing other agents rather than independent evidence, errors propagate and amplify
- **Herding**: when a few high-status agents dominate (in brain.db terms: when Hermes' memories get over-weighted because they come from the CKO)

**Design safeguards:**
- Tag memory confidence based on *evidence quality* (direct observation, inference, hearsay) not *agent seniority*
- Add an independence metric to the aggregation: if N memories on the same topic all come from agents with the same parent in the reporting chain, treat them as a single data point, not N independent data points

### 2.3 Diversity Prediction Theorem (Hong & Page 2004)

"A randomly selected collection of problem solvers outperforms a collection of the best individual problem solvers" — under specific conditions (diversity of perspectives > individual accuracy). This theorem has direct implications for agent routing:

**When to use specialist routing** (best individual): well-defined lookup tasks, known domain, low uncertainty
**When to use diverse ensemble routing** (random sample): novel synthesis tasks, cross-domain questions, high-uncertainty situations

brain.db should support both routing modes. Currently it supports only specialist routing (capability-aware routing to the most relevant agent). Adding an "ensemble query" path — broadcasting a question to a diverse sample of 5–10 agents and aggregating responses — enables collective intelligence for genuinely novel questions.

---

## 3. Transactive Memory Theory — Groups Remember More

### 3.1 Wegner (1987) — Transactive Memory Systems

Wegner's foundational theory: groups develop a **transactive memory system (TMS)** — a shared system for encoding, storing, and retrieving knowledge — where the group uses implicit knowledge of who knows what to achieve greater collective memory than any individual possesses.

Three components of a TMS:
1. **Directory**: who is responsible for which knowledge domain
2. **Allocation**: routing new information to the appropriate expert
3. **Retrieval coordination**: accessing distributed expertise when needed

**This is exactly what 178 agents need and currently lack.**

The `routing_profiles_v1` agent_state key (seen in the live system) is a nascent TMS directory. Weaver's route-context system (COS-83) is an allocation mechanism. What's missing is **retrieval coordination**: when agent A needs knowledge from agent B's domain, there is no mechanism to automatically retrieve it from B's memory records.

**Proposed TMS for brain.db:**

```sql
-- Agent expertise directory (TMS directory component)
CREATE TABLE agent_expertise (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    domain TEXT NOT NULL,                    -- 'security', 'billing', 'frontend', etc.
    expertise_score REAL DEFAULT 0.5,        -- derived from memory density + result quality in this domain
    memory_count INTEGER DEFAULT 0,
    last_active TEXT,
    PRIMARY KEY (agent_id, domain)  -- one row per agent per domain
);
```

This directory enables: "who knows most about topic X?" → route query to top-scoring agents for that domain → synthesize their memories into a collective answer.

### 3.2 Differential Expertise and Division of Labor

TMS research (Moreland 1999, Austin 2003) shows that:
- Groups with clear division of labor develop TMS faster
- Familiarity between group members improves TMS accuracy
- Transactive communication (explicitly encoding "this is for agent X to remember") improves collective memory

**Applied:** Agents should tag memories with the intended beneficiaries when encoding. A `target_agents` field on memories enables proactive push routing: when a memory is encoded, automatically push it to the agents whose expertise directory it most matches.

### 3.3 Transactive Memory in Digital Systems

Sparrow et al. (2011) showed that humans outsource memory to computers (the "Google Effect") — encoding *where* to find information rather than the information itself. 178 agents should exploit this: rather than each agent encoding a complete picture of shared domains, agents should encode **pointers to expertise** ("Sentinel-2 has detailed memories on coherence checking") and retrieve the actual knowledge from the expert agent's memory records on demand.

**Architectural implication:** Add `REFERENCES agents(id)` resolution to retrieval paths — a query can pull memories from a specified agent's scope rather than always searching globally.

---

## 4. Network Effects on Knowledge — Topology Matters

### 4.1 Small-World Networks (Watts & Strogatz 1998)

Small-world networks have:
- Short average path lengths (any two nodes connected in few hops)
- High clustering (neighboring nodes are often connected to each other)

Information propagates fast in small-world networks because there are both dense local clusters (specialists) and long-range connections (bridges between domains).

**The 178-agent network is currently a star topology**: all agents connect through brain.db (the hub). This creates a single point of failure and bottleneck for cross-domain synthesis. A small-world extension would add direct agent-to-agent knowledge edges — not requiring all synthesis to go through the central hub.

**Minimum viable small-world addition:** Add `source_agent_id` to the `knowledge_edges` table. This allows: "find me memories in this domain that are one hop away via agents who have documented cross-domain expertise." The 2,675 existing edges in brain.db could be weighted by source_agent expertise scores to create a navigable small-world graph over agent expertise.

### 4.2 Scale-Free Networks and Knowledge Hubs

Scale-free networks (Barabási 1999) have a few highly-connected hubs and many low-connectivity nodes. In agent memory systems, knowledge hubs naturally emerge: Hermes, openclaw, and kernel have proportionally more memories and more cross-agent references than specialist agents.

**Implication for retrieval:** The existing BFS expansion over knowledge_edges already exploits hub structure. The risk is **hub failure cascade**: if Hermes' memories are stale or wrong, all downstream agents who trust them fail together. Hub confidence should be penalized relative to specialist confidence for within-domain queries (a security specialist should be trusted more than the CKO on security topics, even if the CKO has more total memories).

### 4.3 Information Cascades and Their Prevention

Information cascades occur when agents update beliefs based on observing other agents' behavior rather than independent evidence. If agent A sees that agents B, C, D all have memories supporting conclusion X, A may update to X without independent evidence — and if B, C, D all originally got X from a single (possibly wrong) source, the cascade is an amplification of one error.

**Detection signal:** Memories with the same content originating from different agents but sharing a common `source_agent_id` or `created_at` timestamp cluster are cascade candidates. A cascade detection pass should flag these and mark the redundant memories with lower effective confidence.

**Key paper:** Bikhchandani, S. et al. (1992). "A theory of fads, fashion, custom, and cultural change as informational cascades." *JPE*, 100(5).

---

## 5. Computational Social Choice — When Agents Disagree

### 5.1 Belief Merging Operators

When multiple agents hold contradictory beliefs (detected by coherence_check or contradiction_detection), the system needs a principled way to merge them. Belief merging in social choice theory provides three main approaches:

1. **Majority merging**: accept beliefs held by a majority of agents. Fast, simple, ignores minority insights.
2. **Weighted merging**: weight each agent's belief by their domain expertise score. Better but can over-trust dominant agents.
3. **Distance-based merging (Restall & Priest 1992)**: find the belief set closest (by edit distance) to the majority position that remains consistent. Preserves minority insights while ensuring consistency.

**Recommendation:** Implement weighted merging as default, with distance-based merging as a fallback when weighted merging produces internal contradictions.

```python
def weighted_merge(beliefs: list[Memory], expertise: dict[str, float]) -> Memory:
    """Produce a merged belief weighted by agent domain expertise."""
    weights = [expertise.get(m.agent_id, 0.5) for m in beliefs]
    total_weight = sum(weights)

    # For numeric confidence: weighted average
    merged_confidence = sum(m.confidence * w for m, w in zip(beliefs, weights)) / total_weight

    # For content: return highest-weighted belief, flag as merged
    dominant = max(zip(beliefs, weights), key=lambda x: x[1])[0]
    dominant.confidence = merged_confidence
    dominant.metadata['merge_source'] = [m.id for m in beliefs]
    return dominant
```

### 5.2 Judgment Aggregation and Doctrinal Paradox

The doctrinal paradox (List & Pettit 2002): in multi-issue decisions, majority voting on each issue separately can produce a collectively inconsistent outcome even when each individual agent is internally consistent.

**Applied example:**
- Agent A: "memory spine is healthy" (True) AND "coherence = 0.96" (True) → inference: "architecture is sound" (True)
- Agent B: "memory spine is healthy" (True) AND "coherence = 0.96" (False) → inference: "architecture is sound" (False)
- Agent C: "memory spine is healthy" (False) AND "coherence = 0.96" (True) → inference: "architecture is sound" (False)

Majority vote on each item: "healthy" = True (A+B), "coherence" = True (A+C), "sound" = False (B+C). The majority aggregate is internally inconsistent.

**Implication:** Don't aggregate beliefs issue-by-issue. Aggregate at the conclusion level for inter-related beliefs. This requires identifying which beliefs are logically coupled — a challenge that maps to the `knowledge_edges` contradiction detection work already delivered.

### 5.3 Preserving Minority Insights

Majority aggregation discards minority beliefs. But in 178 heterogeneous agents, the minority agent may be right (especially on domain-specific questions where most agents have low expertise).

**Proposed rule:** Never retire a minority belief to "refuted" status based on head count alone. Instead, mark it `minority_held` and preserve it in the contradiction log. If the majority later proves wrong, the minority belief can be promoted without needing to reconstruct it.

---

## 6. Evolutionary Epistemology — Selection Pressure on Ideas

### 6.1 Memetic Evolution in Agent Systems

Dawkins' meme concept (1976): ideas propagate through populations by replication (memory → retrieval → citation → new memory), variation (each retrieval and re-encoding adds noise/interpretation), and selection (retrieval frequency, citation, coherence score determine which memes survive).

brain.db already implements selection (recall_count, confidence decay) and replication (access_log, knowledge_edges). What's missing is **variation tracking**: when an agent encodes a memory that is semantically similar to but distinct from an existing memory, there should be a `derived_from` link recording the epistemic lineage.

**Memetic fitness function for brain.db:**
```
fitness(memory) = (recall_count × 0.4) + (citation_count × 0.3) + (confidence × 0.2) + (coherence_contribution × 0.1)
```
Where `citation_count` = count of knowledge_edges where this memory is a source, and `coherence_contribution` = +1 if this memory resolved a contradiction, -1 if it caused one.

### 6.2 Selection Pressure Design

Over-strong selection pressure kills diversity (genetic algorithms collapse to local optima). Meme selection in brain.db must preserve conceptual diversity — low-recall minority memories should survive longer than pure fitness sorting would allow.

**Proposed selection rules:**
- Memories with recall_count=0 decay at normal rate (no selection pressure protection)
- Memories with recall_count≥1 from distinct agents get 20% decay reduction (mild selection benefit)
- Memories that are the *only* record in their scope get a permanent "sole source" protection flag: never decay below confidence=0.2 even with zero recall

### 6.3 Directed Mutation — Synthesis Events

Biological evolution is undirected. Memetic evolution in agent systems can be directed: when two similar-but-distinct memories are detected (high cosine similarity but different content), a **synthesis event** can be triggered — asking an agent to reconcile them into a higher-quality combined memory that preserves the insights of both.

This is the machine equivalent of scientific synthesis: individual observations get combined into a more general theory. The result is stored as a new memory with `metadata.synthesis_source = [memory_id_1, memory_id_2]` and both source memories are retained at reduced confidence (they've been superseded by the synthesis).

---

## 7. Collective Intelligence Infrastructure Design

### 7.1 Schema Additions

```sql
-- Agent expertise directory (TMS component)
CREATE TABLE agent_expertise (
    agent_id TEXT REFERENCES agents(id),
    domain TEXT NOT NULL,
    expertise_score REAL DEFAULT 0.5,   -- 0.0–1.0 derived from memory quality in domain
    memory_count INTEGER DEFAULT 0,
    result_count INTEGER DEFAULT 0,     -- successful result events in this domain
    last_active TEXT,
    PRIMARY KEY (agent_id, domain)
);

-- Collective belief aggregation cache
CREATE TABLE collective_beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,                -- scope or query that triggered aggregation
    merged_content TEXT NOT NULL,
    merge_method TEXT NOT NULL,         -- 'weighted', 'majority', 'distance'
    source_memory_ids TEXT,             -- JSON array
    source_agent_ids TEXT,              -- JSON array
    merged_confidence REAL,
    computed_at TEXT NOT NULL,
    valid_until TEXT,                   -- cache TTL
    UNIQUE(topic)
);

-- Memetic lineage tracking
ALTER TABLE memories ADD COLUMN derived_from INTEGER REFERENCES memories(id);
ALTER TABLE memories ADD COLUMN synthesis_sources TEXT;  -- JSON array of memory IDs
ALTER TABLE memories ADD COLUMN sole_source_protected INTEGER DEFAULT 0;

-- Emergence detection log (extends existing emergence_detection.py signals)
CREATE TABLE emergence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emergence_type TEXT NOT NULL,   -- 'convergence', 'cascade_detected', 'synthesis', 'insight_promotion'
    agents_involved TEXT,           -- JSON array of agent IDs
    memory_ids TEXT,                -- JSON array
    description TEXT,
    detected_at TEXT NOT NULL,
    acted_on INTEGER DEFAULT 0
);
```

### 7.2 Capability-Aware Query Routing (TMS Integration)

```python
def route_query_with_tms(query: str, db) -> list[str]:
    """Return ordered list of agent_ids to consult for this query."""
    # 1. Extract query domain signals
    domains = extract_domains(query)  # ['security', 'authentication']

    # 2. Look up expertise directory
    top_agents = db.execute("""
        SELECT agent_id, SUM(expertise_score * memory_count) as relevance_score
        FROM agent_expertise
        WHERE domain IN ({})
        GROUP BY agent_id
        ORDER BY relevance_score DESC
        LIMIT 5
    """.format(','.join('?' * len(domains))), domains).fetchall()

    # 3. If high-confidence specialists found: return them (specialist routing)
    if top_agents and top_agents[0].relevance_score > threshold:
        return [a.agent_id for a in top_agents]

    # 4. Otherwise: return diverse ensemble (5 random agents from different domains)
    return diverse_ensemble_sample(db, n=5, exclude=set(a.agent_id for a in top_agents))
```

### 7.3 Emergence Detection Additions

Extending `07_emergence_detection.py`:

```python
def detect_collective_convergence(db):
    """Detect when multiple agents have independently converged on same insight."""
    # Find memories with cosine_similarity > 0.85 from distinct agents
    convergent_clusters = db.execute("""
        SELECT m1.id as id1, m2.id as id2, m1.agent_id as a1, m2.agent_id as a2
        FROM vec_memories vm1
        JOIN vec_memories vm2 ON vec_distance_cosine(vm1.embedding, vm2.embedding) < 0.15
        JOIN memories m1 ON m1.id = vm1.memory_id
        JOIN memories m2 ON m2.id = vm2.memory_id
        WHERE m1.agent_id != m2.agent_id
          AND m1.status='active' AND m2.status='active'
    """).fetchall()

    for cluster in convergent_clusters:
        log_emergence(db, 'convergence',
            agents=[cluster.a1, cluster.a2],
            memories=[cluster.id1, cluster.id2],
            description=f"Independent convergence detected: agents {cluster.a1} and {cluster.a2}")
```

### 7.4 Collective Reinforcement Cron

Add to consolidation-cycle or as standalone:
```
# Every 12 hours
brainctl meta collective-reinforce --window-hours 24 --min-agents 3
brainctl meta expertise-refresh  # rebuild agent_expertise table from event history
brainctl meta emergence-scan     # run convergence + cascade detection
```

---

## 8. Implementation Priority

| Component | Effort | Dependency | Impact |
|---|---|---|---|
| `agent_expertise` table + pipeline | 2 days | events + memories table | Critical — unlocks TMS routing |
| Collective reinforcement (ACO) | 1 day | access_log multi-agent query | High — emergent salience |
| TMS-aware retrieval routing | 2 days | agent_expertise | High — smarter routing |
| `emergence_events` + convergence detection | 1 day | vec_memories | Medium |
| `collective_beliefs` aggregation | 3 days | agent_expertise + contradiction detect | Medium |
| Memetic lineage (`derived_from`) | 0.5 days | Schema only | Low (foundation for later) |

**Recommended first sprint:** `agent_expertise` table + expertise refresh pipeline. This is the TMS directory and enables all downstream capabilities. Everything else builds on knowing who knows what.

---

## 9. New Questions Raised

1. **Independence vs. shared context**: Wisdom of crowds requires independence. But all 178 agents share brain.db — they are systematically NOT independent (they read each other's memories). Does this mean WoC principles don't apply, or that we need to account for the shared prior in aggregation?

2. **Emergence detection latency**: Emergence by definition appears at a timescale longer than individual events. The current 30-minute consolidation cycle may be too short to detect genuine emergence. What is the minimum observation window for reliable emergence detection?

3. **Agent churn and TMS stability**: When agents are added, removed, or reassigned, the transactive memory directory becomes stale. How do we handle agent turnover without losing the expertise map? (The 22→? agent count trajectory makes this urgent.)

4. **Collective intelligence vs. collective hallucination**: Multiple agents converging on the same wrong answer is worse than one agent being wrong, because it looks like high-confidence consensus. What safeguards prevent collective reinforcement from amplifying errors?

---

## 10. Assumptions Our Architecture Gets Wrong

1. **Global retrieval is the right default**: The current system searches all memories for every query. TMS theory says the right approach is: look up who knows about this, then query their memory specifically. Global search optimizes for recall; TMS routing optimizes for precision + relevance. For a 178-agent system with specialized roles, precision is usually more valuable than exhaustive recall.

2. **All agent contributions are equal**: A memory from a security specialist on security topics should weight higher than the same content from a general-purpose agent. The `confidence` column doesn't encode *source expertise* — it encodes only the encoding agent's self-assessed certainty. These are different things.

3. **Memory is the unit of collective intelligence**: The current architecture treats individual `memories` records as the atoms of collective cognition. But the research suggests the relevant unit is the *memory cluster* — a set of memories from multiple agents on the same topic, aggregated with appropriate weighting. Individual memories are observations; clusters are the collective beliefs.

---

## 11. Highest-Impact Follow-Up Research

**"Agent Expertise Calibration: How Accurately Do Expertise Scores Predict Retrieval Quality?"**

Building the `agent_expertise` table requires a scoring model. The proposed model (memory density × result quality in domain) is a reasonable heuristic, but it conflates *quantity* of experience with *quality* of expertise. The single highest-value research question is: **what is the empirical correlation between our computed expertise scores and actual retrieval quality for domain-specific queries?**

Without this validation, the TMS directory will route queries to the most prolific agents, not the most accurate ones. This is the same calibration problem identified in COS-110 (metacognition) — these two research streams should converge on a shared ground truth: a benchmark query set with known expert answers, run across the agent population to measure both metacognitive calibration and collective routing accuracy.

---

## References

- Surowiecki, J. (2004). *The Wisdom of Crowds*. Doubleday.
- Wegner, D.M. (1987). Transactive memory: A contemporary analysis of the group mind. In *Theories of Group Behavior*. Springer.
- Moreland, R.L. (1999). Transactive memory. In *Shared Cognition in Organizations*. LEA.
- Austin, J.R. (2003). Transactive memory in organizational groups. *Journal of Applied Psychology*, 88(6).
- Sparrow, B. et al. (2011). Google effects on memory. *Science*, 333(6043).
- Hong, L. & Page, S. (2004). Groups of diverse problem solvers can outperform groups of high-ability problem solvers. *PNAS*, 101(46).
- Watts, D.J. & Strogatz, S.H. (1998). Collective dynamics of small-world networks. *Nature*, 393.
- Barabási, A.L. & Albert, R. (1999). Emergence of scaling in random networks. *Science*, 286.
- Bikhchandani, S. et al. (1992). A theory of fads as informational cascades. *JPE*, 100(5).
- List, C. & Pettit, P. (2002). Aggregating sets of judgments. *Economics & Philosophy*, 18(1).
- Dawkins, R. (1976). *The Selfish Gene*. Oxford University Press.
- Restall, G. & Priest, G. (1992). Simplicity in concept formation. *Logique et Analyse*.
