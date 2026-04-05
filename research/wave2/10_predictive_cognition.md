# Predictive Cognition — Anticipating What Agents Will Need Before They Ask

**Research Task:** [COS-112](/COS/issues/COS-112)
**Researcher:** Weaver (Context Integration Engineer)
**Wave:** 2 — Conceptual/Theoretical
**Date:** 2026-03-28
**Deliverable:** Design for a predictive routing engine for 178+ agents

---

## Executive Summary

The highest-value upgrade to Hermes is shifting from reactive retrieval to predictive push. Instead of waiting for `brainctl search`, the memory spine anticipates what each agent needs and pre-loads it. This report synthesizes five research areas into a concrete predictive routing architecture: a **task-embedding + agent-history hybrid model** that scores memory relevance without any explicit query, operates at sub-100ms latency, and can run today on the existing SQLite/brainctl infrastructure.

**Key finding:** Predictive retrieval is not a monolith. Three distinct prediction horizons require different algorithms: *immediate* (what does this agent need right now for this heartbeat?), *session* (what will they likely need in the next 3-5 tool calls?), and *background* (what organizational context should be pre-loaded before they're even assigned this task?).

---

## Research Area 1: Predictive Processing / Free Energy Principle

**Source:** Friston (2010), Active Inference framework; Brown & Friston (2012) on precision-weighted prediction error.

The brain does not passively receive information — it constantly generates predictions and only updates when the prediction error exceeds a threshold. Applied to the memory spine:

- **Each agent has an implicit generative model** of what information it expects to need, shaped by its role and current task.
- **Prediction error = surprise.** When an agent issues `brainctl search`, it's signaling surprise — the expected context wasn't pre-loaded.
- **Goal:** Drive prediction error toward zero by improving the generative model.

**Implication for architecture:** Rather than treating each retrieval as independent, model each agent as having a running expectation state. The predictive engine updates this state after each heartbeat observation. The free energy formulation gives us a natural loss function: minimize the average `brainctl search` calls per heartbeat (surprise signal).

**Precision weighting:** Not all prediction errors are equal. A search by the CEO agent for `billing compliance` should trigger a heavier model update than an IC searching for `git commit format`. Weight prediction errors by agent role importance × task criticality.

---

## Research Area 2: Recommender Systems for Knowledge (Collaborative Filtering)

**Source:** Koren et al. (2009), Matrix Factorization for Recommender Systems; Linden et al. (2003), Amazon item-item filtering.

Collaborative filtering applied to agent knowledge consumption:

### Content-Based Approach
- Each memory chunk has a feature vector (topic, type, project, recency, importance).
- Each agent has a preference profile derived from their retrieval history.
- Score = cosine similarity between memory feature vector and agent preference vector.
- **Strength:** Works from first heartbeat (cold start with role priors).
- **Weakness:** Narrow — can't surface unexpectedly useful cross-domain knowledge.

### Collaborative Filtering Approach
- Build an agent × memory interaction matrix (binary: retrieved or not).
- Use matrix factorization (SVD or ALS) to find latent factors.
- Score = dot product of agent latent vector × memory latent vector.
- **Strength:** Discovers non-obvious connections. "Agents working on auth frequently retrieve session token docs even when searching for rate limiting."
- **Weakness:** Requires historical data (sparse early on). Cold start for new agents.

### Hybrid (Recommended)
For brainctl's scale (178 agents, growing), a **two-stage hybrid**:

1. **Stage 1 — Content-based pre-filter:** Retrieve top-50 candidates by content similarity to agent's current task description.
2. **Stage 2 — Collaborative re-rank:** Re-score those 50 using collaborative signals (what did similar agents retrieve on similar tasks?).

The agent "profile" is derivable from brainctl event history: which memories were retrieved, which were recalled in subsequent heartbeats (utility signal), which tasks were similar.

**Similarity between agents:** Compute via task-type overlap + role hierarchy proximity. `Cipher` and `Sentinel 2` are similar (both security-focused). Retrieved memories by one should up-rank for the other on similar tasks.

---

## Research Area 3: Anticipatory Computing

**Source:** Pejovic & Musolesi (2015), *Anticipatory Mobile Computing: A Survey of the State of the Art and Research Challenges*; Google Now prediction card research.

Anticipatory computing predicts user needs from **contextual features** rather than query text. Key signals:

| Signal | Description | Available in brainctl today? |
|---|---|---|
| Temporal | Time of day, day of week patterns | Yes (event timestamps) |
| Activity | Current task type (impl vs research vs review) | Yes (task title/tags) |
| Sequential | "After X, agents typically need Y" | Partially (event log) |
| Role-based | CTO needs compliance docs after any legal event | Yes (agent role/title) |
| Transition | Post-checkout = high need window | Yes (checkout events) |

**The Post-Checkout Window:** This is the single highest-value prediction moment. Within 2 minutes of checkout, an agent is orienting: reading the issue, scanning for context. This is when predictive push delivers maximum value. The `brainctl push` command in [COS-194](/COS/issues/COS-194) targets exactly this window.

**Sequential patterns that emerged from Wave 1 analysis:** Context from the consolidation cycle shows agents often need:
- Memory type `insight` → followed by `decision` records on same topic
- Memory category `bug` → followed by `workaround` or `fix` on same project
- Any `blocked` status event → trigger push of related `unblock_path` memories

These are predictable transitions that don't require query text.

---

## Research Area 4: Proactive Information Retrieval

**Source:** Research on proactive IR (Teevan et al., 2011); Google Now / Microsoft Cortana research; Hearst (1992) on context-driven retrieval.

Proactive IR without a query requires a **surrogate query**: a representation of the agent's current information need derived from context signals rather than explicit text.

**Surrogate query construction for brainctl:**

```
surrogate_query = (
    0.40 × task_title_embedding
  + 0.25 × task_description_keywords (top 10 tf-idf terms)
  + 0.20 × agent_role_embedding
  + 0.10 × project_context_embedding
  + 0.05 × recent_activity_embedding (last 5 events)
)
```

This vector is computed at checkout time and used to run a latent retrieval over brain.db without any agent interaction. The result is a ranked list of top-K memories pre-loaded into a push buffer.

**Relevance scoring without a query (key metrics):**
- **Topical overlap:** FTS5 keyword match between task text and memory body.
- **Temporal proximity:** Memories created/updated around task creation time score higher.
- **Co-retrieval frequency:** If memories M1 and M2 were retrieved together >3 times, surfacing M1 should co-surface M2.
- **Recency-weighted utility:** `recalled_count` in last 7 days > all-time `recalled_count` signals a trending/relevant memory.

---

## Research Area 5: Temporal Pattern Mining

**Source:** Pei et al. (2001), PrefixSpan; Zaki (2001), SPADE sequential pattern mining; applied to agent workflow logs.

If `Agent X` always searches for `deployment docs` within 2 heartbeats of being assigned an `[Impl-*]` task, this is a sequential pattern worth mining and automating.

**Algorithm choice:** PrefixSpan scales to millions of sequences with low memory overhead. For brainctl's event log (currently small, growing), even a simple frequency-based heuristic captures the high-value patterns:

```sql
-- Find memory M retrieved by 3+ agents within 2 events of task_type T
SELECT task_type, memory_id, COUNT(DISTINCT agent_id) as agent_count
FROM event_sequences
WHERE sequence_gap <= 2
  AND event_type = 'memory_retrieved'
GROUP BY task_type, memory_id
HAVING agent_count >= 3
ORDER BY agent_count DESC;
```

This generates a **pre-fetch rule table**: `IF task_type = "impl" THEN pre-load memory_ids [M1, M2, M7]`.

**Cold-start for new memory types:** Use content-based similarity to bootstrap rules for memories with no retrieval history. A new memory about `rate limiting` inherits the pre-fetch rules from similar memories about `API throttling`.

**Decay for stale rules:** Rules expire if the underlying memories age out of relevance. Apply the same temporal decay from Wave 1 (λ = 0.05 for medium-term) to rule weights.

---

## Research Area 6: Attention Mechanisms for Routing

**Source:** Vaswani et al. (2017), Attention is All You Need; Karpukhin et al. (2020), Dense Passage Retrieval; Izacard & Grave (2021), Leveraging Passage Retrieval.

The key insight from transformer attention: **routing doesn't require a query if you have good key-value representations**.

Applied to memory routing:
- **Key vectors:** Each memory chunk has an embedding (computed once at write time, stored in sqlite-vec).
- **Query vectors:** Each agent has a "context vector" computed from their role + current task.
- **Attention score:** `softmax(Q × K^T / √d)` gives relevance weights.

The agent's query vector doesn't need to be an explicit search — it's computed from:
```
agent_query_vector = embed(f"{agent.role}: {task.title} | {task.description[:200]}")
```

This is essentially DPR (Dense Passage Retrieval) applied to organizational memory. The brainctl `search` command already does this for explicit queries — the predictive layer runs it proactively at checkout.

**Bi-encoder vs cross-encoder tradeoff:**
- Bi-encoder (current brainctl approach): fast, embeddings pre-computed.
- Cross-encoder: higher accuracy, requires real-time joint encoding. Too slow for proactive push.

**Recommendation:** Keep bi-encoder for the predictive layer. Accept ~5% accuracy loss for 10× speed gain. Reserve cross-encoder for explicit, high-stakes searches (CEO / critical decisions).

---

## Predictive Routing Engine — Architecture Design

### System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    PREDICTIVE ROUTING ENGINE                     │
│                                                                  │
│  Trigger: Paperclip checkout event                               │
│                                                                  │
│  ┌──────────────────┐    ┌──────────────────────────────────┐   │
│  │  SURROGATE QUERY  │    │         SCORING PIPELINE         │   │
│  │  CONSTRUCTOR      │    │                                  │   │
│  │                  │    │  Stage 1: Content similarity     │   │
│  │  • task embed    │───▶│  (FTS5 + sqlite-vec, top-50)     │   │
│  │  • role priors   │    │                                  │   │
│  │  • project ctx   │    │  Stage 2: Collaborative re-rank  │   │
│  │  • agent history │    │  (agent-similarity × co-retrieve)│   │
│  └──────────────────┘    │                                  │   │
│                          │  Stage 3: Sequential rules       │   │
│                          │  (pre-fetch rule table match)    │   │
│                          └──────────────┬───────────────────┘   │
│                                         │                        │
│                          ┌──────────────▼───────────────────┐   │
│                          │      TOP-K SELECTOR (K≤5)        │   │
│                          │  • Deduplicate                   │   │
│                          │  • Diversity constraint          │   │
│                          │    (max 2 per memory type)       │   │
│                          │  • Confidence threshold ≥0.60    │   │
│                          └──────────────┬───────────────────┘   │
│                                         │                        │
│                          ┌──────────────▼───────────────────┐   │
│                          │         PUSH BUFFER              │   │
│                          │  Written to push_log table       │   │
│                          │  Expires after 1 heartbeat       │   │
│                          └──────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Data Structures Required

```sql
-- Agent profile table (updated after each heartbeat)
CREATE TABLE IF NOT EXISTS agent_profiles (
    agent_id TEXT NOT NULL,
    role TEXT,
    task_type_history TEXT,  -- JSON: {"impl": 12, "research": 5, ...}
    top_memory_categories TEXT,  -- JSON: top-10 by retrieval count
    latent_vector BLOB,  -- Float32 array (768-dim), updated weekly
    last_updated_at INTEGER,
    PRIMARY KEY (agent_id)
);

-- Pre-fetch rule table (built by temporal pattern miner)
CREATE TABLE IF NOT EXISTS prefetch_rules (
    id INTEGER PRIMARY KEY,
    trigger_task_type TEXT,  -- e.g. "impl", "research", "review"
    trigger_keywords TEXT,   -- JSON array of keywords
    memory_id INTEGER REFERENCES memories(id),
    confidence REAL,         -- 0.0-1.0, decays over time
    support_count INTEGER,   -- how many agents/times triggered
    last_validated_at INTEGER
);

-- Push log (tracks what was pushed and whether it was useful)
CREATE TABLE IF NOT EXISTS push_log (
    id INTEGER PRIMARY KEY,
    agent_id TEXT,
    task_id TEXT,
    memory_id INTEGER REFERENCES memories(id),
    pushed_at INTEGER,
    recalled BOOLEAN DEFAULT FALSE,  -- was it explicitly recalled after push?
    recalled_at INTEGER
);
```

### Implementation Sketch for brainctl

The `brainctl push` command (from [COS-194](/COS/issues/COS-194)) implements the proactive push. The predictive engine feeds it:

```python
def predict_push(agent_id: str, task_description: str, task_type: str, k: int = 5) -> list[Memory]:
    """
    Core predictive routing function.
    Returns top-K memories to push to agent at task checkout.
    """
    # Stage 1: Surrogate query construction
    surrogate = construct_surrogate_query(agent_id, task_description, task_type)

    # Stage 2: Content similarity (existing brainctl search, no query text)
    candidates = vec_search(surrogate.embedding, top_n=50, min_confidence=0.5)

    # Stage 3: Collaborative re-rank
    agent_profile = load_agent_profile(agent_id)
    candidates = collaborative_rerank(candidates, agent_profile, top_n=20)

    # Stage 4: Sequential rule injection
    rule_matches = query_prefetch_rules(task_type, surrogate.keywords)
    candidates = merge_dedup([candidates, rule_matches])

    # Stage 5: Diversity constraint + top-K selection
    final = diversity_select(candidates, k=k, max_per_type=2, min_confidence=0.60)

    # Stage 6: Log to push_log for utility tracking
    log_push(agent_id, task_id, [m.id for m in final])

    return final
```

### Latency Analysis

| Operation | Estimated Latency | Notes |
|---|---|---|
| Surrogate query construction | ~50ms | Embedding via local model or pre-computed role vectors |
| FTS5 content search (top-50) | <5ms | SQLite FTS5, indexed |
| sqlite-vec ANN search (top-50) | <20ms | Approximate nearest neighbor, 768-dim |
| Collaborative re-rank (top-20) | <10ms | Matrix dot product, in-memory agent profiles |
| Sequential rule lookup | <2ms | Indexed SQL query |
| Total | **~90ms** | Well within 100ms target |

**Cold start:** Without agent history, fall back to role-based priors. An agent with role `engineer` and task type `impl` gets the top-5 most-retrieved impl-related memories across all engineers. Degrades gracefully to frequency-based retrieval.

**At 200+ agents:** The push operation is per-checkout, not continuous. Peak load = max concurrent checkouts ≈ 20. At 90ms per push, this is 1.8 seconds of total compute — entirely manageable on a single machine.

---

## Push Notification Architecture

The push buffer must be read by agents at checkout time without explicit `brainctl search`. Three delivery mechanisms:

### Option A: Hook injection (Recommended)
The Paperclip post-checkout hook runs `brainctl push --task-id $TASK_ID --agent-id $AGENT_ID` and writes results to a temp file read by the next heartbeat's system context. **Zero agent behavior change required.**

### Option B: Inline heartbeat context enrichment
Paperclip's `GET /api/issues/:id/heartbeat-context` response includes a `suggestedMemories` array. Agents must read and use it. **Requires all agents to be updated.**

### Option C: Pre-populated system prompt injection
Results are injected into the agent's system prompt via the harness before heartbeat starts. **Highest impact, most invasive.**

**Recommendation:** Start with Option A (hook injection). It's transparent, reversible, and requires no agent code changes. Migrate to Option C once utility is proven (push utility rate > 40%).

---

## Temporal Modeling

**Problem:** A memory about a Q1 security incident is highly relevant during the incident response but should decay to low relevance afterward. The existing Wave 1 decay handles general aging — the predictive layer needs *task-relative* recency.

**Solution:** Add a `task_proximity_score` to the push scoring:

```python
task_proximity_score = (
    0.6 × recency_relative_to_task_creation   # newer = more relevant to current context
  + 0.4 × project_overlap_score               # memories from same project score higher
)
```

**Temporal clusters:** Group memories by creation date into rolling windows (1d, 7d, 30d, 90d). When an agent is assigned a task in a project, heavily weight memories created in the 7-day window before the task was created — these represent the context that motivated the task.

---

## Evaluation Metrics

| Metric | Formula | Target |
|---|---|---|
| Push utility rate | `pushed memories recalled / total pushed` | > 40% |
| Search reduction | `(baseline searches - post-push searches) / baseline` | > 30% |
| Precision@5 | `relevant in top-5 / 5` | > 0.60 |
| Cold-start precision@5 | Same, for agents with < 5 heartbeats | > 0.40 |
| Latency P99 | Push computation time | < 200ms |

**Measuring "relevant":** A pushed memory is relevant if the agent either (a) explicitly retrieves it in the same heartbeat, or (b) incorporates content semantically similar to it in their output (harder to measure, proxy with topic overlap).

---

## New Questions Raised

1. **How do we handle adversarial prediction poisoning?** If an agent's retrieval history is gamed (e.g., a compromised heartbeat retrieves irrelevant memories to skew the profile), the collaborative filter could push misleading context to similar agents. Need anomaly detection on retrieval patterns.

2. **What's the right K?** Pushing 5 memories assumes all 5 are within the agent's context budget. But some agents (e.g., CEO) process dense tasks with rich system prompts. The push budget should be agent-role and task-complexity-adaptive, not a fixed K.

3. **Does predictive push create epistemic bubbles?** If Hermes consistently pushes the same "high-utility" memories, do agents stop searching for novel context? The recommender filter bubble problem applied to organizational knowledge.

4. **Feedback loop stability:** If memories that get pushed get retrieved more (utility signal), and retrieval boosts their push priority, we get a rich-get-richer loop. Low-utility-but-important memories (e.g., rarely-triggered compliance rules) may get starved. Need an "exploration budget" — push 1 random high-confidence but low-retrieved memory per session.

---

## Assumptions That May Be Wrong

1. **"Fewer brainctl searches = better"** — This assumes explicit search is a cost to minimize. But some searches are exploratory and intentional. A system that eliminates all searches eliminates discovery. Measure reduction in *redundant* searches, not total searches.

2. **"Agent role is a stable predictor"** — The role taxonomy (engineer, manager, CEO) is coarse. An engineer working on a security task has very different needs than one on a UI task. Task-type matters more than role for short-horizon prediction. Role matters more for background pre-loading.

3. **"sqlite-vec is fast enough for 200+ agents"** — At 200 concurrent checkouts (worst case), 200 × 50ms vec searches = 10 seconds of sequential SQLite I/O. SQLite with WAL mode handles concurrent reads, but write contention during profile updates could be a bottleneck. May need to shard agent profiles from the memories table.

---

## Highest-Impact Follow-Up Research

**Single recommendation:** **Memory Granularity Calibration** (Wave 4 candidate #2 in FRONTIER.md).

The predictive engine's precision is bounded by memory chunk quality. A memory that spans three unrelated topics can't be relevantly pushed — it'll match weakly on all three and confidently on none. Before investing in sophisticated prediction models, establishing the right memory unit of granularity would compound the value of every retrieval and push operation in the system. The 10% granularity improvement in chunking translates directly to 10%+ improvement in push precision@5 — with zero model changes.

---

## References

- Friston, K. (2010). The free-energy principle: a unified brain theory? *Nature Reviews Neuroscience*.
- Koren, Y., Bell, R., & Volinsky, C. (2009). Matrix factorization techniques for recommender systems. *IEEE Computer*.
- Pejovic, V., & Musolesi, M. (2015). Anticipatory mobile computing. *ACM Computing Surveys*.
- Pei, J. et al. (2001). PrefixSpan: Mining sequential patterns efficiently. *ICDE*.
- Vaswani, A. et al. (2017). Attention is all you need. *NeurIPS*.
- Karpukhin, V. et al. (2020). Dense passage retrieval for open-domain question answering. *EMNLP*.
- Teevan, J. et al. (2011). Slow search: Information retrieval without time constraints. *HCIR*.
